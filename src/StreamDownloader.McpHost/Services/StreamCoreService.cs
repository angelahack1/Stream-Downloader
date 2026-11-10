using System.Reflection;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using StreamDownloader.Models;
using Microsoft.Extensions.Configuration;
using Microsoft.Extensions.Hosting;
using Microsoft.Extensions.Logging;

namespace StreamDownloader.Services;

public sealed class StreamCoreService : BackgroundService
{
    private static readonly HashSet<string> TerminalStates = new(StringComparer.Ordinal)
    {
        "completed_verified", "failed_finalization", "failed_download", "outcome_unknown"
    };

    private readonly StreamDownloaderOptions _options;
    private readonly StateStore _store;
    private readonly PathPolicy _pathPolicy;
    private readonly NetworkInspector _networkInspector;
    private readonly YtDlpService _ytDlp;
    private readonly ILogger<StreamCoreService> _logger;
    private readonly Dictionary<string, Task> _monitors = new(StringComparer.Ordinal);
    private readonly object _monitorGate = new();
    private readonly SemaphoreSlim _downloadGate = new(1, 1);
    private readonly MachineFacts _machine;
    private readonly int _slotCount;
    private readonly string _slotReason;
    private readonly SemaphoreSlim _downloadSlots;
    private CancellationTokenSource? _stoppingTokenSource;

    public StreamCoreService(IConfiguration configuration, ILogger<StreamCoreService> logger)
    {
        _logger = logger;
        _options = configuration.GetSection("StreamDownloader").Get<StreamDownloaderOptions>() ?? new StreamDownloaderOptions();
        _options.ExpandEnvironmentVariables();
        _pathPolicy = new PathPolicy(_options.GetAllowedRoots(), _options.RestrictToAllowedRoots);
        _networkInspector = new NetworkInspector(_options.BlockPrivateNetworkTargets);
        _store = new StateStore(Path.GetFullPath(_options.StateDirectory));
        _ytDlp = new YtDlpService(_options, logger);
        // Ask the machine before deciding what to run on it, and say what was decided and why.
        _machine = MachineProfile.Probe(_options.DefaultDownloadDirectory);
        (_slotCount, _slotReason) = MachineProfile.PlanSlots(_machine, _options.MaxConcurrentDownloads);
        _downloadSlots = new SemaphoreSlim(_slotCount, _slotCount);
        _logger.LogInformation("Machine: {Machine}. {Slots}", _machine.Summary, _slotReason);
    }

    public McpResponse<CapabilitiesData> GetCapabilities()
    {
        var toolInfo = _ytDlp.GetToolInfo();
        var siteExtraction = new SiteExtractionCapability(
            toolInfo.YtDlpPath is not null,
            toolInfo.YtDlpPath,
            toolInfo.YtDlpVersion,
            toolInfo.FfmpegPath is not null,
            toolInfo.FfmpegPath,
            YtDlpService.QualityPresets,
            toolInfo.JavaScriptRuntime);
        var resources = MachineProfile.PlanDownload(_machine, _options.ConcurrentFragments, _options.ChildProcessPriority);
        var machine = new MachineCapability(
            _machine.LogicalProcessors,
            _machine.MemoryBytes,
            MachineProfile.Label(_machine.Storage),
            _machine.Summary,
            _slotCount,
            resources.ConcurrentFragments,
            resources.ChildPriority.ToString(),
            $"{_slotReason} For the default download volume: {resources.Reason} Each job re-plans for its own destination volume.");
        var data = new CapabilitiesData(
            "Stream-Downloader",
            Assembly.GetExecutingAssembly().GetName().Version?.ToString(3) ?? "1.0.0",
            RuntimeInformation.FrameworkDescription,
            RuntimeInformation.OSDescription,
            siteExtraction,
            machine,
            [
                "stream_capabilities", "stream_download", "stream_download_status", "stream_download_list",
                "stream_download_wait", "stream_download_retry"
            ],
            _pathPolicy.AllowedRoots,
            Path.GetDirectoryName(_store.StateFile) ?? _options.StateDirectory);

        var siteReady = toolInfo.FfmpegPath is not null &&
            (toolInfo.YtDlpPath is not null || _options.AutoAcquireYtDlp);
        var code = siteReady ? "ready" : "not_ready";
        var message = siteReady
            ? "yt-dlp site extraction is ready for watch pages (e.g. YouTube)."
            : "FFmpeg was not found, or yt-dlp is missing while AutoAcquireYtDlp is disabled.";
        return siteReady ? McpResponse<CapabilitiesData>.Success(code, message, data)
            : McpResponse<CapabilitiesData>.Failure(code, message, data);
    }

    public async Task<McpResponse<JobView>> GetStatusAsync(string jobId, CancellationToken cancellationToken)
    {
        var job = await GetJobRecordAsync(jobId, cancellationToken).ConfigureAwait(false);
        if (job?.State == "completed_verified" &&
            (!File.Exists(job.FinalPath) || new FileInfo(job.FinalPath).Length != job.ObservedBytes))
            return McpResponse<JobView>.Failure("completed_output_changed", "The previously verified output is missing or has a different size. Completion evidence is historical; use a new key to download again.", ToJobView(job));
        return job is null
            ? McpResponse<JobView>.Failure("job_not_found", "Job ID was not found.")
            : McpResponse<JobView>.Success("job_status", "Current durable job status.", ToJobView(job));
    }

    public async Task<McpResponse<JobListData>> ListAsync(string? stateFilter, int limit, CancellationToken cancellationToken)
    {
        limit = Math.Clamp(limit, 1, 100);
        var records = await _store.ReadAsync(state => state.Jobs.Values
            .Where(job => string.IsNullOrWhiteSpace(stateFilter) || job.State.Equals(stateFilter, StringComparison.OrdinalIgnoreCase))
            .OrderByDescending(job => job.CreatedAt)
            .Select(ToJobView)
            .ToArray(), cancellationToken).ConfigureAwait(false);
        return McpResponse<JobListData>.Success("job_list", "Sanitized jobs owned by Stream-Downloader.", new JobListData(records.Take(limit).ToArray(), records.Length));
    }

    public async Task<McpResponse<JobView>> WaitAsync(
        string jobId,
        long afterVersion,
        int timeoutSeconds,
        CancellationToken cancellationToken)
    {
        timeoutSeconds = Math.Clamp(timeoutSeconds, 1, 45);
        var deadline = DateTimeOffset.UtcNow.AddSeconds(timeoutSeconds);
        while (DateTimeOffset.UtcNow < deadline)
        {
            var job = await GetJobRecordAsync(jobId, cancellationToken).ConfigureAwait(false);
            if (job is null)
            {
                return McpResponse<JobView>.Failure("job_not_found", "Job ID was not found.");
            }

            if (job.StateVersion > afterVersion || IsTerminal(job.State))
            {
                return await GetStatusAsync(jobId, cancellationToken).ConfigureAwait(false);
            }

            await Task.Delay(500, cancellationToken).ConfigureAwait(false);
        }

        var unchanged = await GetJobRecordAsync(jobId, cancellationToken).ConfigureAwait(false);
        return unchanged is null
            ? McpResponse<JobView>.Failure("job_not_found", "Job ID was not found.")
            : McpResponse<JobView>.Success("unchanged", "No state change occurred before the bounded wait expired.", ToJobView(unchanged));
    }

    public async Task<McpResponse<JobView>> RetryAsync(string jobId, string newIdempotencyKey, CancellationToken cancellationToken)
    {
        var job = await GetJobRecordAsync(jobId, cancellationToken).ConfigureAwait(false);
        if (job is null)
        {
            return McpResponse<JobView>.Failure("job_not_found", "Job ID was not found.");
        }

        if (job.State is not ("failed_finalization" or "failed_download"))
        {
            return McpResponse<JobView>.Failure("job_not_retryable", "Only definitively failed terminal jobs can be retried.");
        }
        if (newIdempotencyKey == job.IdempotencyKey)
            return McpResponse<JobView>.Failure("new_key_required", "Retry requires a different idempotency key.");

        string url;
        try
        {
            url = SecretVault.Unprotect(job.ProtectedUrl);
        }
        catch (Exception ex)
        {
            return McpResponse<JobView>.Failure("source_unavailable", $"Stored source URL could not be recovered: {ex.Message}");
        }

        return await DownloadAsync(
            url,
            job.DestinationDirectory,
            string.IsNullOrWhiteSpace(job.FileName) ? null : job.FileName,
            job.Quality ?? _options.DefaultQuality,
            job.Container,
            newIdempotencyKey,
            cancellationToken).ConfigureAwait(false);
    }

    public async Task<McpResponse<JobView>> DownloadAsync(
        string url,
        string? destination,
        string? fileName,
        string quality,
        string? format,
        string? idempotencyKey,
        CancellationToken cancellationToken)
    {
        await _downloadGate.WaitAsync(cancellationToken).ConfigureAwait(false);
        try { return await DownloadInternalAsync(url, destination, fileName, quality, format, idempotencyKey, cancellationToken).ConfigureAwait(false); }
        finally { _downloadGate.Release(); }
    }

    private async Task<McpResponse<JobView>> DownloadInternalAsync(string url, string? destination, string? fileName,
        string quality, string? format, string? idempotencyKey, CancellationToken cancellationToken)
    {
        try
        {
            quality = NormalizeChoice(quality, YtDlpService.QualityPresets, _options.DefaultQuality);
            format = string.IsNullOrWhiteSpace(format) ? null : format.Trim().TrimStart('.').ToLowerInvariant();

            // A destination can be a directory (C:\Videos) or an exact file path (C:\Videos\MyVideo01.mp4).
            var target = ResolveTarget(destination, fileName, format);
            var fingerprint = DeterministicKey(url, target.Directory.ToUpperInvariant(),
                target.FileName?.ToUpperInvariant() ?? "", quality, target.Container ?? "");
            var key = idempotencyKey ?? fingerprint;
            if (string.IsNullOrWhiteSpace(key) || key.Length is < 8 or > 128)
                return McpResponse<JobView>.Failure("invalid_idempotency_key", "Idempotency key must be 8 to 128 characters.");
            var existing = await _store.ReadAsync(state => state.Idempotency.TryGetValue(key, out var id)
                ? state.Jobs.GetValueOrDefault(id) : null, cancellationToken).ConfigureAwait(false);
            if (existing is not null)
            {
                if (existing.RequestFingerprint != fingerprint)
                    return McpResponse<JobView>.Failure("idempotency_key_conflict", "This key belongs to another request. Use a new key for a new request.");
                if (existing.State == "completed_verified" && !File.Exists(existing.FinalPath))
                    return McpResponse<JobView>.Failure("completed_file_missing", "Previously completed output is missing. Use a new key to download again; history is retained.", ToJobView(existing));
                return McpResponse<JobView>.Success("idempotent_replay", "Returning the original job without submitting again.", ToJobView(existing));
            }

            target = target with { Container = NormalizeChoice(target.Container ?? "",
                quality == "audio" ? new[] { "mp3", "m4a", "flac", "wav", "opus", "aac" } : new[] { "mp4", "mkv", "webm" },
                quality == "audio" ? "mp3" : "mp4") };

            return await StartSiteDownloadAsync(
                url, target, quality, key, fingerprint, cancellationToken).ConfigureAwait(false);
        }
        catch (Exception ex) when (ex is not OperationCanceledException)
        {
            _logger.LogWarning("Download routing failed: {Message}", ex.Message);
            return McpResponse<JobView>.Failure("download_failed", ex.Message);
        }
    }

    /// <summary>
    /// Resolves a caller-supplied destination (directory or full file path) plus an optional filename
    /// and format into a concrete directory, optional filename, and container. When the destination is
    /// an exact file path its extension wins as the container so the produced file matches the name.
    /// </summary>
    private DownloadTarget ResolveTarget(string? destination, string? fileName, string? format)
    {
        string directory;
        string? name = string.IsNullOrWhiteSpace(fileName) ? null : fileName;
        string? container = format;

        if (string.IsNullOrWhiteSpace(destination))
        {
            directory = _options.DefaultDownloadDirectory;
        }
        else
        {
            var trimmed = destination.Trim();
            var looksLikeFile = HasFileExtension(trimmed) && !EndsWithSeparator(trimmed) && !Directory.Exists(trimmed);
            if (looksLikeFile)
            {
                directory = Path.GetDirectoryName(trimmed) ?? _options.DefaultDownloadDirectory;
                name = Path.GetFileName(trimmed);
                var extension = Path.GetExtension(trimmed).TrimStart('.').ToLowerInvariant();
                if (!string.IsNullOrEmpty(extension))
                {
                    // The exact filename the user named dictates the container.
                    container = extension;
                }
            }
            else
            {
                directory = trimmed;
            }
        }

        var validatedDirectory = _pathPolicy.ValidateDirectory(directory, _options.DefaultDownloadDirectory);
        var validatedName = name is null ? null : _pathPolicy.ValidateFileName(name);
        if (validatedName is not null && Path.HasExtension(validatedName))
            container = Path.GetExtension(validatedName).TrimStart('.').ToLowerInvariant();
        Directory.CreateDirectory(validatedDirectory);
        validatedDirectory = _pathPolicy.ValidateDirectory(validatedDirectory, _options.DefaultDownloadDirectory);
        return new DownloadTarget(validatedDirectory, validatedName, container);
    }

    private static bool HasFileExtension(string path)
    {
        var lastSegment = Path.GetFileName(path.TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar));
        var extension = Path.GetExtension(lastSegment);
        return extension.Length > 1;
    }

    private static bool EndsWithSeparator(string path) =>
        path.EndsWith(Path.DirectorySeparatorChar) || path.EndsWith(Path.AltDirectorySeparatorChar);

    private async Task<McpResponse<JobView>> StartSiteDownloadAsync(
        string url,
        DownloadTarget target,
        string quality,
        string key,
        string fingerprint,
        CancellationToken cancellationToken)
    {
        // Readiness (which may download yt-dlp on first use) happens inside the background runner so
        // this call returns promptly. Here we only validate the request and commit the job.
        await _networkInspector.ValidatePublicUrlAsync(url, cancellationToken).ConfigureAwait(false);
        var uri = new Uri(url);

        var directory = target.Directory;
        var safeName = target.FileName;
        EnsureDiskSpace(directory, null);

        var jobId = NewId("job");
        var stagingDirectory = Path.Combine(directory, ".stream-downloader-staging", jobId);
        _pathPolicy.ValidateDirectory(stagingDirectory, _options.DefaultDownloadDirectory);
        Directory.CreateDirectory(stagingDirectory);
        _pathPolicy.ValidateDirectory(stagingDirectory, _options.DefaultDownloadDirectory);
        var now = DateTimeOffset.UtcNow;
        var job = new JobRecord
        {
            JobId = jobId,
            IdempotencyKey = key,
            ProtectedUrl = SecretVault.Protect(uri.AbsoluteUri),
            DestinationDirectory = directory,
            FileName = safeName ?? string.Empty,
            StagingDirectory = stagingDirectory,
            State = "queued",
            RequestFingerprint = fingerprint,
            SourcePageHost = uri.DnsSafeHost,
            Quality = quality,
            Container = target.Container,
            CreatedAt = now,
            UpdatedAt = now,
            StateVersion = 1,
            Events = [new JobEvent(now, "queued", "job_committed", "Site-extraction job committed; yt-dlp will start shortly.")]
        };

        var inserted = await _store.MutateAsync(state =>
        {
            if (state.Idempotency.ContainsKey(key))
            {
                return false;
            }

            state.Jobs[job.JobId] = job;
            state.Idempotency[key] = job.JobId;
            return true;
        }, cancellationToken).ConfigureAwait(false);
        if (!inserted)
        {
            return McpResponse<JobView>.Failure("idempotency_race", "Another request claimed the idempotency key.");
        }

        EnsureSiteRunner(job.JobId, StoppingToken);
        var committed = await GetJobRecordAsync(job.JobId, cancellationToken).ConfigureAwait(false) ?? job;
        return McpResponse<JobView>.Success(
            "site_download_started",
            "yt-dlp is resolving and downloading the video. Poll stream_download_status or stream_download_wait for completion.",
            ToJobView(committed));
    }

    private void EnsureSiteRunner(string jobId, CancellationToken stoppingToken)
    {
        lock (_monitorGate)
        {
            if (_monitors.TryGetValue(jobId, out var existing) && !existing.IsCompleted)
            {
                return;
            }

            _monitors[jobId] = Task.Run(() => RunYtDlpJobAsync(jobId, stoppingToken), CancellationToken.None);
        }
    }

    private async Task RunYtDlpJobAsync(string jobId, CancellationToken stoppingToken)
    {
        var holdsSlot = false;
        try
        {
            var job = await GetJobRecordAsync(jobId, stoppingToken).ConfigureAwait(false);
            if (job is null || IsTerminal(job.State))
            {
                return;
            }

            string url = SecretVault.Unprotect(job.ProtectedUrl);

            // Simultaneous downloads are sized from this machine: on a modest one, a single transfer
            // beats several fighting over one disk head and four cores.
            if (!await _downloadSlots.WaitAsync(0, stoppingToken).ConfigureAwait(false))
            {
                await TransitionAsync(jobId, "queued", "waiting_for_slot",
                    $"Waiting for a free download slot. {_slotReason}", null, null, stoppingToken).ConfigureAwait(false);
                await _downloadSlots.WaitAsync(stoppingToken).ConfigureAwait(false);
            }
            holdsSlot = true;

            // Provision yt-dlp here (not on the caller's thread) so stream_download returns immediately.
            await TransitionAsync(jobId, "preparing", "tool_check", "Ensuring yt-dlp and ffmpeg are available.", null, null, stoppingToken)
                .ConfigureAwait(false);
            try
            {
                await _ytDlp.EnsureReadyAsync(stoppingToken).ConfigureAwait(false);
            }
            catch (OperationCanceledException) when (stoppingToken.IsCancellationRequested)
            {
                throw;
            }
            catch (Exception ex)
            {
                await TransitionAsync(jobId, "failed_download", "tool_unavailable", ex.Message, "tool_unavailable", ex.Message, CancellationToken.None)
                    .ConfigureAwait(false);
                return;
            }

            var resources = MachineProfile.PlanDownload(MachineProfile.Probe(job.StagingDirectory),
                _options.ConcurrentFragments, _options.ChildProcessPriority);
            await TransitionAsync(jobId, "downloading", "yt_dlp_started",
                $"yt-dlp is resolving and transferring the media with {resources.ConcurrentFragments} concurrent fragment(s) at {resources.ChildPriority} priority: {resources.Reason}",
                null, null, stoppingToken).ConfigureAwait(false);

            var lastFlush = DateTimeOffset.MinValue;
            var progressGate = new object();

            void OnProgress(YtDlpProgress progress)
            {
                bool flush;
                lock (progressGate)
                {
                    var now = DateTimeOffset.UtcNow;
                    flush = now - lastFlush >= TimeSpan.FromSeconds(1);
                    if (flush)
                    {
                        lastFlush = now;
                    }
                }

                if (flush)
                {
                    _ = BumpSiteProgressAsync(jobId, progress);
                }
            }

            YtDlpDownloadResult result;
            try
            {
                result = await _ytDlp.DownloadAsync(
                    url, job.StagingDirectory, job.Quality ?? _options.DefaultQuality, job.Container, resources, OnProgress, stoppingToken)
                    .ConfigureAwait(false);
            }
            catch (OperationCanceledException) when (stoppingToken.IsCancellationRequested)
            {
                throw;
            }
            catch (Exception ex)
            {
                await TransitionAsync(jobId, "failed_download", "yt_dlp_failed", ex.Message, "yt_dlp_failed", ex.Message, CancellationToken.None)
                    .ConfigureAwait(false);
                return;
            }

            await TransitionAsync(jobId, "verifying", "verification_started", "Download finished; verifying and finalizing.", null, null, CancellationToken.None)
                .ConfigureAwait(false);
            await FinalizeSiteFileAsync(jobId, result, CancellationToken.None).ConfigureAwait(false);
        }
        catch (OperationCanceledException) when (stoppingToken.IsCancellationRequested)
        {
            await TransitionAsync(jobId, "failed_download", "host_stopped", "Host stopped; retry with a new key.",
                "host_stopped", "Site download was interrupted by host shutdown.", CancellationToken.None).ConfigureAwait(false);
        }
        catch (Exception ex)
        {
            _logger.LogError(ex, "yt-dlp job {JobId} crashed.", jobId);
            await TransitionAsync(jobId, "failed_download", "yt_dlp_crashed", ex.Message, "yt_dlp_crashed", ex.Message, CancellationToken.None)
                .ConfigureAwait(false);
        }
        finally
        {
            if (holdsSlot)
            {
                _downloadSlots.Release();
            }
        }
    }

    private async Task FinalizeSiteFileAsync(string jobId, YtDlpDownloadResult result, CancellationToken cancellationToken)
    {
        var job = await GetJobRecordAsync(jobId, cancellationToken).ConfigureAwait(false);
        if (job is null)
        {
            return;
        }

        var producedExtension = Path.GetExtension(result.ProducedFilePath);
        string desiredName;
        if (!string.IsNullOrWhiteSpace(job.FileName))
        {
            desiredName = Path.HasExtension(job.FileName) ? job.FileName : job.FileName + producedExtension;
        }
        else
        {
            desiredName = Path.GetFileName(result.ProducedFilePath);
        }

        string finalName;
        try
        {
            finalName = _pathPolicy.ValidateFileName(desiredName);
        }
        catch (Exception)
        {
            if (!string.IsNullOrWhiteSpace(job.FileName)) throw;
            finalName = _pathPolicy.ValidateFileName("video_" + job.JobId + producedExtension);
        }

        finalName = ResolveConflict(job.DestinationDirectory, finalName);

        try
        {
            await FinalizeFileAsync(job, result.ProducedFilePath, finalName, result, cancellationToken).ConfigureAwait(false);
        }
        catch (Exception ex)
        {
            await HandleFinalizationFailureAsync(jobId, ex).ConfigureAwait(false);
        }
    }

    private async Task BumpSiteProgressAsync(string jobId, YtDlpProgress progress)
    {
        try
        {
            await _store.MutateAsync(state =>
            {
                if (!state.Jobs.TryGetValue(jobId, out var job) || job.State != "downloading")
                {
                    return false;
                }

                if (progress.DownloadedBytes is long downloaded)
                {
                    job.ObservedBytes = downloaded;
                }

                // yt-dlp totals describe individual tracks, not the final merged container.
                job.StreamPercent = progress.Percent;

                job.UpdatedAt = DateTimeOffset.UtcNow;
                job.StateVersion++;
                return true;
            }, CancellationToken.None).ConfigureAwait(false);
        }
        catch (Exception ex)
        {
            _logger.LogDebug("Progress update skipped for {JobId}: {Message}", jobId, ex.Message);
        }
    }

    private static void TryCleanupStaging(string stagingDirectory)
    {
        try
        {
            if (Directory.Exists(stagingDirectory))
            {
                // Never recursively remove an unvalidated tree (or another job's partial files).
                if (!Directory.EnumerateFileSystemEntries(stagingDirectory).Any())
                    Directory.Delete(stagingDirectory, recursive: false);
            }

            // Also remove the shared ".stream-downloader-staging" parent once it holds no other jobs, so the
            // user's destination folder is left clean.
            var parent = Path.GetDirectoryName(stagingDirectory);
            if (parent is not null &&
                Path.GetFileName(parent).Equals(".stream-downloader-staging", StringComparison.OrdinalIgnoreCase) &&
                Directory.Exists(parent) &&
                !Directory.EnumerateFileSystemEntries(parent).Any())
            {
                Directory.Delete(parent, recursive: false);
            }
        }
        catch (Exception)
        {
            // Leftover staging is harmless; ignore cleanup failures.
        }
    }

    private static string DeterministicKey(params string[] parts)
    {
        var material = System.Text.Json.JsonSerializer.Serialize(parts);
        var hash = SHA256.HashData(System.Text.Encoding.UTF8.GetBytes(material));
        return "auto_" + Convert.ToHexString(hash)[..40].ToLowerInvariant();
    }

    private async Task RecoverAsync(CancellationToken stoppingToken)
    {
        var recoverable = await _store.ReadAsync(state => state.Jobs.Values
            .Where(job => !IsTerminal(job.State))
            .Select(job => job.JobId)
            .ToArray(), stoppingToken).ConfigureAwait(false);
        foreach (var jobId in recoverable)
        {
            var recovering = await GetJobRecordAsync(jobId, stoppingToken).ConfigureAwait(false);
            if (recovering?.State == "finalizing")
            {
                await RecoverFinalizationAsync(recovering, stoppingToken).ConfigureAwait(false);
                continue;
            }

            // A forced process crash may leave a child alive. Do not invite duplicate transfers.
            await TransitionAsync(
                jobId, "outcome_unknown", "interrupted_by_restart",
                "An unclean restart interrupted tracking. Inspect the old child process/staging before a new submission.",
                "interrupted_by_restart", "Previous site-process outcome cannot be proven after an unclean restart.",
                stoppingToken).ConfigureAwait(false);
        }

    }

    protected override async Task ExecuteAsync(CancellationToken stoppingToken) =>
        await Task.Delay(Timeout.InfiniteTimeSpan, stoppingToken).ConfigureAwait(ConfigureAwaitOptions.SuppressThrowing);

    public override async Task StartAsync(CancellationToken cancellationToken)
    {
        _stoppingTokenSource = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
        // Complete recovery before MCP transport starts accepting new jobs; never mistake a new
        // request for a transfer interrupted by a previous host instance.
        await RecoverAsync(StoppingToken).ConfigureAwait(false);
        await base.StartAsync(cancellationToken).ConfigureAwait(false);
    }

    public override async Task StopAsync(CancellationToken cancellationToken)
    {
        if (_stoppingTokenSource is not null)
        {
            await _stoppingTokenSource.CancelAsync().ConfigureAwait(false);
        }

        await base.StopAsync(cancellationToken).ConfigureAwait(false);
        Task[] pending;
        lock (_monitorGate) pending = _monitors.Values.ToArray();
        await Task.WhenAll(pending).WaitAsync(cancellationToken).ConfigureAwait(false);
    }

    public override void Dispose()
    {
        base.Dispose();
        _stoppingTokenSource?.Dispose();
        _store.Dispose();
    }

    private async Task HandleFinalizationFailureAsync(string jobId, Exception error)
    {
        var current = await GetJobRecordAsync(jobId, CancellationToken.None).ConfigureAwait(false);
        if (current?.State == "finalizing")
            await RecoverFinalizationAsync(current, CancellationToken.None).ConfigureAwait(false);
        else
            await TransitionAsync(jobId, "failed_finalization", "finalization_failed", error.Message,
                "finalization_failed", error.Message, CancellationToken.None).ConfigureAwait(false);
    }

    private async Task FinalizeFileAsync(JobRecord job, string source, string finalName,
        YtDlpDownloadResult media, CancellationToken cancellationToken)
    {
        _pathPolicy.ValidateDirectory(job.DestinationDirectory, _options.DefaultDownloadDirectory);
        _pathPolicy.ValidateDirectory(job.StagingDirectory, _options.DefaultDownloadDirectory);
        if (!Path.GetDirectoryName(Path.GetFullPath(source))!.Equals(Path.GetFullPath(job.StagingDirectory), StringComparison.OrdinalIgnoreCase) ||
            (File.GetAttributes(source) & FileAttributes.ReparsePoint) != 0)
            throw new IOException("Unexpected staged file location or reparse point.");
        finalName = _pathPolicy.ValidateFileName(finalName);
        var destination = Path.Combine(job.DestinationDirectory, finalName);
        // Deny writes while hashing and moving, but allow the rename of our own open handle.
        await using var stream = new FileStream(source, FileMode.Open, FileAccess.Read, FileShare.Delete,
            1024 * 1024, FileOptions.Asynchronous | FileOptions.SequentialScan);
        var length = stream.Length;
        if (length > _options.MaximumSingleFileBytes) throw new IOException("Output exceeds configured size ceiling.");
        var hash = Convert.ToHexString(await SHA256.HashDataAsync(stream, cancellationToken).ConfigureAwait(false)).ToLowerInvariant();
        if (File.Exists(destination)) throw new IOException("Destination already exists; overwrite refused.");
        await _store.MutateAsync(state =>
        {
            var current = state.Jobs[job.JobId];
            current.FileName = finalName;
            current.ObservedBytes = length;
            current.ObservedSha256 = hash;
            current.FinalizingSource = source;
            current.SourceTitle = media.Title;
            current.Media = media.Media;
            current.StreamPercent = null;
            SetState(current, "finalizing", "move_intent_saved", "Hash, length and destination saved before moving the file.");
            return true;
        }, cancellationToken).ConfigureAwait(false);
        File.Move(source, destination, overwrite: false);
        await TransitionAsync(job.JobId, "completed_verified", "file_finalized", "Verified file moved to the requested destination.",
            null, null, CancellationToken.None).ConfigureAwait(false);
        TryCleanupStaging(job.StagingDirectory);
    }

    private async Task RecoverFinalizationAsync(JobRecord job, CancellationToken cancellationToken)
    {
        try
        {
            _pathPolicy.ValidateDirectory(job.DestinationDirectory, _options.DefaultDownloadDirectory);
            _pathPolicy.ValidateDirectory(job.StagingDirectory, _options.DefaultDownloadDirectory);
            _pathPolicy.ValidateFileName(job.FileName);
            var path = File.Exists(job.FinalPath) ? job.FinalPath : job.FinalizingSource;
            if (path is null || !File.Exists(path) ||
                (!path.Equals(job.FinalPath, StringComparison.OrdinalIgnoreCase) &&
                 !Path.GetDirectoryName(Path.GetFullPath(path))!.Equals(Path.GetFullPath(job.StagingDirectory), StringComparison.OrdinalIgnoreCase)) ||
                (File.GetAttributes(path) & FileAttributes.ReparsePoint) != 0 ||
                new FileInfo(path).Length != job.ObservedBytes ||
                await ComputeSha256Async(path, cancellationToken).ConfigureAwait(false) != job.ObservedSha256)
                throw new IOException("Finalization recovery could not verify the saved hash and size.");
            if (!path.Equals(job.FinalPath, StringComparison.OrdinalIgnoreCase)) File.Move(path, job.FinalPath, overwrite: false);
            await TransitionAsync(job.JobId, "completed_verified", "finalization_recovered", "Recovered the durable move intent and verified the output.",
                null, null, cancellationToken).ConfigureAwait(false);
            TryCleanupStaging(job.StagingDirectory);
        }
        catch (Exception ex) when (ex is not OperationCanceledException)
        {
            await TransitionAsync(job.JobId, "outcome_unknown", "recovery_failed", ex.Message,
                "recovery_failed", ex.Message, CancellationToken.None).ConfigureAwait(false);
        }
    }

    private async Task TransitionAsync(
        string jobId,
        string stateName,
        string code,
        string message,
        string? failureCode,
        string? failureMessage,
        CancellationToken cancellationToken)
    {
        await _store.MutateAsync(state =>
        {
            if (!state.Jobs.TryGetValue(jobId, out var job))
            {
                return false;
            }

            SetState(job, stateName, code, message);
            job.FailureCode = failureCode;
            job.FailureMessage = failureMessage;
            return true;
        }, cancellationToken).ConfigureAwait(false);
    }

    private static void SetState(JobRecord job, string stateName, string code, string message)
    {
        job.State = stateName;
        job.UpdatedAt = DateTimeOffset.UtcNow;
        job.StateVersion++;
        job.Events.Add(new JobEvent(job.UpdatedAt, stateName, code, message));
        if (job.Events.Count > 50)
        {
            job.Events.RemoveRange(0, job.Events.Count - 50);
        }
    }

    private Task<JobRecord?> GetJobRecordAsync(string jobId, CancellationToken cancellationToken) =>
        _store.ReadAsync(state => state.Jobs.GetValueOrDefault(jobId), cancellationToken);

    private static JobView ToJobView(JobRecord job) =>
        new(job.JobId, job.State, IsTerminal(job.State), job.FinalPath,
            IsTerminal(job.State) ? null : job.StagingDirectory, job.SourcePageHost, job.SourceTitle, job.Quality, job.Container,
            job.ObservedBytes, job.StreamPercent, job.ObservedSha256,
            job.FailureCode, job.FailureMessage, job.CreatedAt, job.UpdatedAt, job.StateVersion, job.Events.ToArray(), job.Media);

    private static string ResolveConflict(string directory, string fileName)
    {
        if (!File.Exists(Path.Combine(directory, fileName)))
        {
            return fileName;
        }

        var stem = Path.GetFileNameWithoutExtension(fileName);
        var extension = Path.GetExtension(fileName);
        for (var i = 1; i <= 9999; i++)
        {
            var candidate = $"{stem} ({i}){extension}";
            if (!File.Exists(Path.Combine(directory, candidate)))
            {
                return candidate;
            }
        }

        throw new IOException("Unable to allocate a non-conflicting filename.");
    }

    private void EnsureDiskSpace(string directory, long? expectedBytes)
    {
        var root = Path.GetPathRoot(directory) ?? throw new InvalidOperationException("Destination drive could not be determined.");
        var drive = new DriveInfo(root);
        var required = expectedBytes is long bytes
            ? Math.Max(512L * 1024 * 1024, bytes + bytes / 5)
            : 512L * 1024 * 1024;
        if (drive.AvailableFreeSpace < required)
        {
            throw new IOException($"Insufficient free space. Required safety budget: {required} bytes.");
        }
    }

    private static async Task<string> ComputeSha256Async(string path, CancellationToken cancellationToken)
    {
        await using var stream = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.Read, 1024 * 1024,
            FileOptions.Asynchronous | FileOptions.SequentialScan);
        var hash = await SHA256.HashDataAsync(stream, cancellationToken).ConfigureAwait(false);
        return Convert.ToHexString(hash).ToLowerInvariant();
    }

    private static string NormalizeChoice(string value, IReadOnlyCollection<string> allowed, string defaultValue)
    {
        var normalized = string.IsNullOrWhiteSpace(value) ? defaultValue : value.Trim().ToLowerInvariant();
        if (!allowed.Contains(normalized))
        {
            throw new InvalidOperationException($"Unsupported value '{value}'. Allowed: {string.Join(", ", allowed)}.");
        }

        return normalized;
    }

    private static bool IsTerminal(string state) => TerminalStates.Contains(state);
    private static string NewId(string prefix) => $"{prefix}_{Guid.NewGuid():N}";
    private CancellationToken StoppingToken => _stoppingTokenSource?.Token ?? CancellationToken.None;
}
