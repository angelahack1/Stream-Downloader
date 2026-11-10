using System.Diagnostics;
using System.Globalization;
using System.Security.Cryptography;
using System.Text.Json;
using System.Text.RegularExpressions;
using Microsoft.Extensions.Logging;

namespace StreamDownloader.Services;

/// <summary>
/// Wraps the yt-dlp command-line tool so the MCP can resolve watch pages (YouTube and every other
/// site yt-dlp supports) into real media files, downloading and merging separate video+audio
/// streams with ffmpeg. A watch page is not a media file, so this is the component that makes
/// "download the video at &lt;watch page&gt;" actually work.
/// </summary>
internal sealed class YtDlpService
{
    private const string ProgressPrefix = "@@STREAMDL_PROGRESS@@";
    private const string FilePrefix = "@@STREAMDL_FILE@@";

    private readonly StreamDownloaderOptions _options;
    private readonly ILogger _logger;
    private readonly SemaphoreSlim _acquireGate = new(1, 1);

    private string? _resolvedYtDlp;
    private string? _resolvedFfmpeg;
    private string? _cachedVersion;

    public YtDlpService(StreamDownloaderOptions options, ILogger logger)
    {
        _options = options;
        _logger = logger;
    }

    public static IReadOnlyList<string> QualityPresets =>
        ["best", "2160p", "1440p", "1080p", "720p", "480p", "360p", "240p", "144p", "audio"];

    /// <summary>
    /// Non-destructive capability probe for stream_capabilities. Never downloads anything and never
    /// spawns yt-dlp (its cold start can be slow); the version is whatever was cached by a prior
    /// EnsureReadyAsync, otherwise null.
    /// </summary>
    public YtDlpToolInfo GetToolInfo() =>
        new(LocateExistingYtDlp(), _cachedVersion, LocateFfmpeg(), LocateJavaScriptRuntime());

    /// <summary>
    /// Ensures yt-dlp is available, acquiring the official release binary when configured to.
    /// Returns the resolved executable paths. Thread-safe.
    /// </summary>
    public async Task<YtDlpToolInfo> EnsureReadyAsync(CancellationToken cancellationToken)
    {
        var cachedYtDlp = _resolvedYtDlp;
        if (cachedYtDlp is not null && File.Exists(cachedYtDlp))
        {
            var cachedFfmpeg = _resolvedFfmpeg ?? LocateFfmpeg();
            _resolvedFfmpeg = cachedFfmpeg;
            return new YtDlpToolInfo(cachedYtDlp, _cachedVersion, cachedFfmpeg, LocateJavaScriptRuntime());
        }

        await _acquireGate.WaitAsync(cancellationToken).ConfigureAwait(false);
        try
        {
            var ytDlpPath = _resolvedYtDlp ?? LocateExistingYtDlp();
            if (ytDlpPath is null)
            {
                if (!_options.AutoAcquireYtDlp)
                {
                    throw new InvalidOperationException(
                        "yt-dlp was not found and AutoAcquireYtDlp is disabled. Install yt-dlp (e.g. 'winget install yt-dlp') " +
                        "or set StreamDownloader:YtDlpPath in appsettings.json.");
                }

                ytDlpPath = await AcquireYtDlpAsync(cancellationToken).ConfigureAwait(false);
            }

            var ffmpegPath = _resolvedFfmpeg ?? LocateFfmpeg();

            // Cache the version once (this runs on the background job, so a slow cold start is fine).
            _cachedVersion ??= await ReadVersionAsync(ytDlpPath, cancellationToken).ConfigureAwait(false);
            if (string.IsNullOrWhiteSpace(_cachedVersion))
                throw new YtDlpException("yt-dlp did not report a usable version.");

            _resolvedYtDlp = ytDlpPath;
            _resolvedFfmpeg = ffmpegPath;

            _logger.LogInformation("yt-dlp ready: {Path} ({Version}); ffmpeg: {Ffmpeg}",
                ytDlpPath, _cachedVersion, ffmpegPath ?? "(auto)");
            return new YtDlpToolInfo(ytDlpPath, _cachedVersion, ffmpegPath, LocateJavaScriptRuntime());
        }
        finally
        {
            _acquireGate.Release();
        }
    }

    /// <summary>
    /// Downloads the media at <paramref name="url"/> into <paramref name="stagingDirectory"/>, merging
    /// video+audio with ffmpeg. Returns the produced file. Progress is streamed to <paramref name="onProgress"/>.
    /// </summary>
    public async Task<YtDlpDownloadResult> DownloadAsync(
        string url,
        string stagingDirectory,
        string quality,
        string? container,
        DownloadResources resources,
        Action<YtDlpProgress> onProgress,
        CancellationToken cancellationToken)
    {
        var tool = await EnsureReadyAsync(cancellationToken).ConfigureAwait(false);
        if (tool.FfmpegPath is null)
            throw new YtDlpException("FFmpeg is required to enforce the requested output format.");
        var ffprobe = Path.Combine(Path.GetDirectoryName(tool.FfmpegPath)!, "ffprobe.exe");
        if (!File.Exists(ffprobe)) ffprobe = SearchPath("ffprobe.exe")
            ?? throw new YtDlpException("ffprobe is required to verify the downloaded media.");
        Directory.CreateDirectory(stagingDirectory);

        var audioOnly = string.Equals(quality, "audio", StringComparison.OrdinalIgnoreCase);
        var outputTemplate = Path.Combine(stagingDirectory, "%(title).150B [%(id)s].%(ext)s");

        var args = new List<string>
        {
            "--ignore-config",
            "--no-color",
            "--newline",
            "--no-playlist",
            "--no-mtime",
            "--no-simulate",
            "--no-plugin-dirs",
            "--windows-filenames",
            "--socket-timeout", "30",
            "--max-filesize", _options.MaximumSingleFileBytes.ToString(CultureInfo.InvariantCulture),
            "--print", $"after_move:{FilePrefix}%(filepath)j",
            "--retries", "10",
            "--fragment-retries", "10",
            "--concurrent-fragments", resources.ConcurrentFragments.ToString(CultureInfo.InvariantCulture),
            "--progress-template",
            $"download:{ProgressPrefix} %(progress._percent_str)s|%(progress.downloaded_bytes)s|%(progress.total_bytes)s|%(progress.total_bytes_estimate)s",
            "-o", outputTemplate
        };

        if (!string.IsNullOrWhiteSpace(tool.FfmpegPath))
        {
            args.Add("--ffmpeg-location");
            args.Add(tool.FfmpegPath!);
        }
        if (tool.JavaScriptRuntime is not null)
            args.AddRange(["--js-runtimes", tool.JavaScriptRuntime]);

        if (audioOnly)
        {
            var audioFormat = string.IsNullOrWhiteSpace(container) ? "mp3" : container!.ToLowerInvariant();
            args.AddRange(["-f", "bestaudio/best", "-x", "--audio-format", audioFormat, "--audio-quality", "0"]);
        }
        else
        {
            args.AddRange(["-f", BuildVideoFormat(quality, container)]);
            args.Add("--merge-output-format");
            args.Add(string.IsNullOrWhiteSpace(container) ? "mp4" : container!.ToLowerInvariant());
            args.AddRange(["--remux-video", container ?? "mp4"]);
        }

        args.Add("--");
        args.Add(url);

        string? produced = null;
        var stderrTail = new List<string>();
        var exitCode = await RunProcessAsync(
            tool.YtDlpPath!,
            args,
            line =>
            {
                if (line.StartsWith(FilePrefix, StringComparison.Ordinal))
                    produced = JsonSerializer.Deserialize<string>(line[FilePrefix.Length..]);
                else HandleOutputLine(line, onProgress);
            },
            line =>
            {
                HandleOutputLine(line, onProgress);
                if (!line.StartsWith(ProgressPrefix, StringComparison.Ordinal))
                {
                    stderrTail.Add(line);
                    if (stderrTail.Count > 25)
                    {
                        stderrTail.RemoveAt(0);
                    }
                }
            },
            TimeSpan.FromSeconds(Math.Max(60, _options.SiteDownloadTimeoutSeconds)),
            resources.ChildPriority,
            cancellationToken).ConfigureAwait(false);

        if (exitCode != 0)
        {
            var detail = stderrTail.Count > 0
                ? string.Join(" | ", stderrTail.TakeLast(4))
                : $"yt-dlp exited with code {exitCode}.";
            // Extractor diagnostics may contain signed URLs, cookies or proxy credentials.
            detail = Regex.Replace(detail, @"https?://[^\s|]+", "[URL redacted]", RegexOptions.IgnoreCase);
            throw new YtDlpException($"yt-dlp failed (exit {exitCode}): {detail}");
        }

        if (produced is null || !File.Exists(produced) ||
            !string.Equals(Path.GetDirectoryName(Path.GetFullPath(produced)), Path.GetFullPath(stagingDirectory), StringComparison.OrdinalIgnoreCase))
            throw new YtDlpException("yt-dlp did not report a single completed output inside this job's staging directory.");
        var info = new FileInfo(produced);
        if (info.Length <= 0 || info.Length > _options.MaximumSingleFileBytes)
            throw new YtDlpException("Output is empty or exceeds MaximumSingleFileBytes.");
        var media = await VerifyMediaAsync(ffprobe, produced, quality, container ?? (audioOnly ? "mp3" : "mp4"), cancellationToken).ConfigureAwait(false);
        var title = DeriveTitle(Path.GetFileNameWithoutExtension(produced));
        return new YtDlpDownloadResult(produced, title, info.Length, media);
    }

    private static void HandleOutputLine(string line, Action<YtDlpProgress> onProgress)
    {
        if (ParseProgressLine(line) is { } progress)
        {
            onProgress(progress);
        }
    }

    internal static YtDlpProgress? ParseProgressLine(string line)
    {
        if (!line.StartsWith(ProgressPrefix, StringComparison.Ordinal))
        {
            return null;
        }

        var payload = line[ProgressPrefix.Length..].Trim();
        var parts = payload.Split('|');
        if (parts.Length < 4)
        {
            return null;
        }

        double? percent = null;
        var percentText = parts[0].Trim().TrimEnd('%').Trim();
        if (double.TryParse(percentText, NumberStyles.Float, CultureInfo.InvariantCulture, out var pct))
        {
            percent = pct;
        }

        var downloaded = ParseByteField(parts[1]);
        var total = ParseByteField(parts[2]) ?? ParseByteField(parts[3]);
        return new YtDlpProgress(percent, downloaded, total);
    }

    private static long? ParseByteField(string value)
    {
        value = value.Trim();
        return long.TryParse(value, NumberStyles.Integer, CultureInfo.InvariantCulture, out var parsed) ? parsed : null;
    }

    internal static string BuildVideoFormat(string quality, string? container)
    {
        // Prefer an mp4/m4a (H.264/AAC) pairing for broad compatibility, then fall back to any
        // best video+audio, then to any single progressive stream.
        var heightCap = quality.ToLowerInvariant() switch
        {
            "2160p" => 2160,
            "1440p" => 1440,
            "1080p" => 1080,
            "720p" => 720,
            "480p" => 480,
            "360p" => 360,
            "240p" => 240,
            "144p" => 144,
            "best" => 0,
            _ => throw new ArgumentException("Unsupported video quality.", nameof(quality))
        };

        var wantsMp4 = string.IsNullOrWhiteSpace(container) ||
                       container!.Equals("mp4", StringComparison.OrdinalIgnoreCase);

        if (heightCap == 0)
        {
            return wantsMp4
                ? "bv*[ext=mp4]+ba[ext=m4a]/bv*+ba/b"
                : "bv*+ba/b";
        }

        var filter = $"[height<=?{heightCap}]";
        return wantsMp4
            ? $"bv*{filter}[ext=mp4]+ba[ext=m4a]/bv*{filter}+ba/b{filter}"
            : $"bv*{filter}+ba/b{filter}";
    }

    private static string DeriveTitle(string fileStem)
    {
        // Output template is "<title> [<id>]"; strip the trailing " [id]" if present.
        var trimmed = fileStem.Trim();
        var open = trimmed.LastIndexOf(" [", StringComparison.Ordinal);
        if (open > 0 && trimmed.EndsWith(']'))
        {
            return trimmed[..open].Trim();
        }

        return trimmed;
    }

    private static async Task<StreamDownloader.Models.MediaVerification> VerifyMediaAsync(string ffprobe, string path,
        string quality, string container, CancellationToken cancellationToken)
    {
        var lines = new List<string>();
        var code = await RunProcessAsync(ffprobe,
            ["-v", "error", "-show_format", "-show_streams", "-of", "json", path],
            lines.Add, _ => { }, TimeSpan.FromSeconds(30), cancellationToken).ConfigureAwait(false);
        if (code != 0) throw new YtDlpException("ffprobe could not read the output media.");
        using var document = JsonDocument.Parse(string.Join('\n', lines));
        var root = document.RootElement;
        var actual = root.GetProperty("format").GetProperty("format_name").GetString() ?? "";
        var duration = double.TryParse(root.GetProperty("format").GetProperty("duration").GetString(),
            CultureInfo.InvariantCulture, out var seconds) ? seconds : 0;
        var streams = root.GetProperty("streams").EnumerateArray().ToArray();
        var video = streams.Where(s => s.GetProperty("codec_type").GetString() == "video" &&
            (!s.TryGetProperty("disposition", out var d) || !d.TryGetProperty("attached_pic", out var a) || a.GetInt32() == 0)).ToArray();
        var audio = streams.Any(s => s.GetProperty("codec_type").GetString() == "audio");
        int? height = video.Length > 0 ? video.Max(s => s.GetProperty("height").GetInt32()) : null;
        var expectedDemuxer = container switch { "mp4" or "m4a" => "mp4", "mkv" => "matroska", "opus" => "ogg", _ => container };
        if (!actual.Split(',').Contains(expectedDemuxer) ||
            !Path.GetExtension(path).Equals("." + container, StringComparison.OrdinalIgnoreCase))
            throw new YtDlpException($"Output container did not match requested {container}.");
        if (!double.IsFinite(duration) || duration <= 0 || (quality == "audio" ? !audio || video.Length != 0 : height is null or <= 0))
            throw new YtDlpException("Output does not contain the requested playable media streams.");
        if (quality != "audio" && quality != "best" &&
            int.TryParse(quality.TrimEnd('p'), out var cap) && height > cap)
            throw new YtDlpException($"Output height {height} exceeds requested maximum {cap}.");
        return new(actual, height, audio, duration);
    }

    private string? LocateJavaScriptRuntime()
    {
        var configured = _options.JavaScriptRuntime.Trim();
        if (configured.Length == 0 || configured.Equals("none", StringComparison.OrdinalIgnoreCase)) return null;
        if (!configured.Equals("auto", StringComparison.OrdinalIgnoreCase)) return configured;
        var deno = SearchPath("deno.exe");
        if (deno is not null) return "deno:" + deno;
        var node = SearchPath("node.exe");
        return node is null ? null : "node:" + node;
    }

    private string? LocateExistingYtDlp()
    {
        if (!string.IsNullOrWhiteSpace(_options.YtDlpPath) && File.Exists(_options.YtDlpPath))
        {
            return Path.GetFullPath(_options.YtDlpPath);
        }

        var toolsCopy = Path.Combine(Path.GetFullPath(_options.ToolsDirectory), "yt-dlp.exe");
        if (File.Exists(toolsCopy))
        {
            return toolsCopy;
        }

        return SearchPath("yt-dlp.exe") ?? SearchPath("yt-dlp");
    }

    private string? LocateFfmpeg()
    {
        if (!string.IsNullOrWhiteSpace(_options.FfmpegPath) && File.Exists(_options.FfmpegPath))
        {
            return Path.GetFullPath(_options.FfmpegPath);
        }

        var onPath = SearchPath("ffmpeg.exe") ?? SearchPath("ffmpeg");
        if (onPath is not null)
        {
            return onPath;
        }

        // Fall back to common install locations so this works even when the launching process
        // (e.g. an MCP host) does not pass a PATH that includes ffmpeg.
        foreach (var candidate in CommonFfmpegLocations())
        {
            if (File.Exists(candidate))
            {
                return candidate;
            }
        }

        return null;
    }

    private IEnumerable<string> CommonFfmpegLocations()
    {
        var localAppData = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
        var programData = Environment.GetEnvironmentVariable("ProgramData") ?? @"C:\ProgramData";
        var programFiles = Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles);

        yield return Path.Combine(programData, "chocolatey", "bin", "ffmpeg.exe");
        yield return Path.Combine(localAppData, "Microsoft", "WinGet", "Links", "ffmpeg.exe");
        yield return Path.Combine(programFiles, "ffmpeg", "bin", "ffmpeg.exe");
        yield return Path.Combine(Path.GetFullPath(_options.ToolsDirectory), "ffmpeg.exe");
    }

    private static string? SearchPath(string fileName)
    {
        var pathVar = Environment.GetEnvironmentVariable("PATH");
        if (string.IsNullOrEmpty(pathVar))
        {
            return null;
        }

        foreach (var segment in pathVar.Split(Path.PathSeparator, StringSplitOptions.RemoveEmptyEntries))
        {
            try
            {
                var candidate = Path.Combine(segment.Trim(), fileName);
                if (File.Exists(candidate))
                {
                    return candidate;
                }
            }
            catch (ArgumentException)
            {
                // Malformed PATH segment; ignore.
            }
        }

        return null;
    }

    private async Task<string> AcquireYtDlpAsync(CancellationToken cancellationToken)
    {
        var toolsDirectory = Path.GetFullPath(_options.ToolsDirectory);
        Directory.CreateDirectory(toolsDirectory);
        var target = Path.Combine(toolsDirectory, "yt-dlp.exe");
        var temp = target + ".download";

        _logger.LogInformation("Acquiring yt-dlp from {Url}", _options.YtDlpDownloadUrl);
        using var httpClient = new HttpClient(new HttpClientHandler { AllowAutoRedirect = true })
        {
            Timeout = TimeSpan.FromMinutes(5)
        };
        httpClient.DefaultRequestHeaders.UserAgent.ParseAdd("Stream-Downloader/1.0");

        using (var response = await httpClient
                   .GetAsync(_options.YtDlpDownloadUrl, HttpCompletionOption.ResponseHeadersRead, cancellationToken)
                   .ConfigureAwait(false))
        {
            response.EnsureSuccessStatusCode();
            await using var source = await response.Content.ReadAsStreamAsync(cancellationToken).ConfigureAwait(false);
            await using var destination = new FileStream(
                temp, FileMode.Create, FileAccess.Write, FileShare.None, 1024 * 1024, FileOptions.Asynchronous);
            await source.CopyToAsync(destination, cancellationToken).ConfigureAwait(false);
        }

        var checksums = await httpClient.GetStringAsync(_options.YtDlpChecksumsUrl, cancellationToken).ConfigureAwait(false);
        await VerifyChecksumAsync(temp, checksums, cancellationToken).ConfigureAwait(false);
        File.Move(temp, target, overwrite: false);

        // Fail fast if the downloaded binary does not actually run.
        string version;
        try
        {
            version = await ReadVersionAsync(target, cancellationToken).ConfigureAwait(false);
            if (string.IsNullOrWhiteSpace(version))
                throw new YtDlpException("Downloaded yt-dlp.exe did not report a version; the acquisition may be corrupt.");
        }
        catch
        {
            // Preserve the failed binary for diagnosis without allowing discovery on the next run.
            File.Move(target, target + ".rejected-" + Guid.NewGuid().ToString("N"), overwrite: false);
            throw;
        }

        _cachedVersion = version;
        _logger.LogInformation("Acquired yt-dlp {Version} at {Path}", version, target);
        return target;
    }

    internal static async Task VerifyChecksumAsync(string file, string checksums, CancellationToken cancellationToken)
    {
        var entries = checksums.Split('\n').Select(line => line.Split((char[]?)null, StringSplitOptions.RemoveEmptyEntries))
            .Where(parts => parts.Length == 2 && parts[1].TrimStart('*') == "yt-dlp.exe").ToArray();
        await using var stream = File.OpenRead(file);
        var hash = Convert.ToHexString(await SHA256.HashDataAsync(stream, cancellationToken).ConfigureAwait(false));
        if (entries.Length != 1 || !hash.Equals(entries[0][0], StringComparison.OrdinalIgnoreCase))
            throw new YtDlpException("yt-dlp checksum verification failed; downloaded binary was NOT executed.");
    }

    private static async Task<string> ReadVersionAsync(string ytDlpPath, CancellationToken cancellationToken)
    {
        string? version = null;
        var code = await RunProcessAsync(ytDlpPath, ["--version"], line => version ??= line.Trim(), _ => { },
            TimeSpan.FromSeconds(30), cancellationToken).ConfigureAwait(false);
        return code == 0 ? version ?? string.Empty : string.Empty;
    }

    internal static Task<int> RunProcessAsync(
        string fileName,
        IReadOnlyList<string> arguments,
        Action<string> onStdout,
        Action<string> onStderr,
        TimeSpan timeout,
        CancellationToken cancellationToken) =>
        RunProcessAsync(fileName, arguments, onStdout, onStderr, timeout, null, cancellationToken);

    internal static async Task<int> RunProcessAsync(
        string fileName,
        IReadOnlyList<string> arguments,
        Action<string> onStdout,
        Action<string> onStderr,
        TimeSpan timeout,
        ProcessPriorityClass? priority,
        CancellationToken cancellationToken)
    {
        var startInfo = new ProcessStartInfo(fileName)
        {
            RedirectStandardInput = true,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            UseShellExecute = false,
            CreateNoWindow = true
        };
        foreach (var argument in arguments)
        {
            startInfo.ArgumentList.Add(argument);
        }

        using var process = new Process { StartInfo = startInfo, EnableRaisingEvents = true };
        Exception? outputError = null;
        void Receive(Action<string> handler, string line)
        {
            try { handler(line); }
            catch (Exception ex)
            {
                Interlocked.CompareExchange(ref outputError, ex, null);
                TryKill(process);
            }
        }
        process.OutputDataReceived += (_, e) =>
        {
            if (e.Data is not null)
            {
                Receive(onStdout, e.Data);
            }
        };
        process.ErrorDataReceived += (_, e) =>
        {
            if (e.Data is not null)
            {
                Receive(onStderr, e.Data);
            }
        };

        if (!process.Start())
        {
            throw new YtDlpException($"Failed to start process: {fileName}");
        }

        // A lowered priority class is inherited by the processes this child launches (FFmpeg), so
        // the whole download leaves the desktop responsive. Never raised above Normal.
        if (priority is { } requested && requested != ProcessPriorityClass.Normal)
        {
            TrySetPriority(process, requested);
        }

        // Child tools must never inherit the MCP transport's live input pipe. They require no
        // stdin, and waiting on or consuming that pipe can stall an otherwise idle download.
        process.StandardInput.Close();
        process.BeginOutputReadLine();
        process.BeginErrorReadLine();

        using var timeoutSource = new CancellationTokenSource(timeout);
        using var linked = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken, timeoutSource.Token);
        try
        {
            await process.WaitForExitAsync(linked.Token).ConfigureAwait(false);
        }
        catch (OperationCanceledException)
        {
            TryKill(process);
            await process.WaitForExitAsync(CancellationToken.None).WaitAsync(TimeSpan.FromSeconds(10)).ConfigureAwait(false);
            if (timeoutSource.IsCancellationRequested && !cancellationToken.IsCancellationRequested)
            {
                throw new YtDlpException($"yt-dlp exceeded the {timeout.TotalSeconds:F0}s time budget and was aborted.");
            }

            throw;
        }

        if (outputError is not null)
            throw new YtDlpException($"Subprocess output could not be processed ({outputError.GetType().Name}).");
        return process.ExitCode;
    }

    private static void TrySetPriority(Process process, ProcessPriorityClass priority)
    {
        try
        {
            if (!process.HasExited)
            {
                process.PriorityClass = priority;
            }
        }
        catch (Exception)
        {
            // Best effort: the child may already have exited.
        }
    }

    private static void TryKill(Process process)
    {
        try
        {
            if (!process.HasExited)
            {
                process.Kill(entireProcessTree: true);
            }
        }
        catch (Exception)
        {
            // Best-effort cleanup.
        }
    }
}

internal sealed record YtDlpToolInfo(string? YtDlpPath, string? YtDlpVersion, string? FfmpegPath, string? JavaScriptRuntime);

internal sealed record YtDlpDownloadResult(string ProducedFilePath, string Title, long? Bytes, StreamDownloader.Models.MediaVerification Media);

internal sealed record YtDlpProgress(double? Percent, long? DownloadedBytes, long? TotalBytes);

internal sealed class YtDlpException(string message) : Exception(message);
