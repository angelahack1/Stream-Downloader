namespace StreamDownloader.Services;

public sealed class StreamDownloaderOptions
{
    public string StateDirectory { get; set; } = @"%LOCALAPPDATA%\Stream-Downloader\state";
    public string DefaultDownloadDirectory { get; set; } = @"%USERPROFILE%\Downloads";
    public List<string> AdditionalAllowedRoots { get; set; } = [];
    public long MaximumSingleFileBytes { get; set; } = 100L * 1024 * 1024 * 1024;
    public bool BlockPrivateNetworkTargets { get; set; } = true;

    // This is a private, single-user tool. By default it downloads to any absolute local path the
    // user names (UNC paths and reparse-point tricks are still rejected for basic safety). Set this
    // to true to confine downloads to AllowedRoots instead.
    public bool RestrictToAllowedRoots { get; set; } = false;

    // Absolute path to yt-dlp.exe. Empty means: search the local tools directory, then PATH,
    // then (if AutoAcquireYtDlp) download the official release into the tools directory.
    public string YtDlpPath { get; set; } = string.Empty;

    // Absolute path to ffmpeg.exe (needed to merge DASH video+audio). Empty means auto-detect on PATH.
    public string FfmpegPath { get; set; } = string.Empty;

    // auto selects an installed Deno or Node; empty/none leaves yt-dlp defaults. No runtime install.
    // May also be an explicit yt-dlp spec, e.g. node:C:\Program Files\nodejs\node.exe.
    public string JavaScriptRuntime { get; set; } = "auto";

    // When yt-dlp cannot be located, download the official release binary on first use.
    public bool AutoAcquireYtDlp { get; set; } = true;

    // Official yt-dlp Windows binary. The "latest" download endpoint redirects to the newest release asset.
    public string YtDlpDownloadUrl { get; set; } =
        "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp.exe";

    // Local tools directory where an auto-acquired yt-dlp.exe is stored.
    public string ToolsDirectory { get; set; } = @"%LOCALAPPDATA%\Stream-Downloader\tools";

    // Default quality label for site extraction when the caller does not specify one.
    public string DefaultQuality { get; set; } = "best";

    // Hard ceiling (seconds) for a single yt-dlp download before it is aborted.
    public int SiteDownloadTimeoutSeconds { get; set; } = 3600;
    public string YtDlpChecksumsUrl { get; set; } =
        "https://github.com/yt-dlp/yt-dlp/releases/latest/download/SHA2-256SUMS";

    // Hardware-aware sizing (see MachineProfile). 0 / "auto" asks this machine at run time: its
    // processors, its memory and whether the destination volume pays a seek penalty. Any explicit
    // value is obeyed exactly, even when it is a poor fit for the machine.
    public int ConcurrentFragments { get; set; } = 0;
    public int MaxConcurrentDownloads { get; set; } = 0;
    public string ChildProcessPriority { get; set; } = "auto";

    public void ExpandEnvironmentVariables()
    {
        StateDirectory = Environment.ExpandEnvironmentVariables(StateDirectory);
        DefaultDownloadDirectory = Environment.ExpandEnvironmentVariables(DefaultDownloadDirectory);
        ToolsDirectory = Environment.ExpandEnvironmentVariables(ToolsDirectory);
        YtDlpPath = Environment.ExpandEnvironmentVariables(YtDlpPath);
        FfmpegPath = Environment.ExpandEnvironmentVariables(FfmpegPath);
        JavaScriptRuntime = Environment.ExpandEnvironmentVariables(JavaScriptRuntime);
        AdditionalAllowedRoots = AdditionalAllowedRoots
            .Select(Environment.ExpandEnvironmentVariables)
            .ToList();
    }

    public IReadOnlyList<string> GetAllowedRoots() =>
        new[] { DefaultDownloadDirectory }
            .Concat(AdditionalAllowedRoots)
            .Select(Path.GetFullPath)
            .Distinct(StringComparer.OrdinalIgnoreCase)
            .ToArray();
}
