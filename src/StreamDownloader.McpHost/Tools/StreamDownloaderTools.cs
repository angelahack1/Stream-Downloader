using System.ComponentModel;
using StreamDownloader.Models;
using StreamDownloader.Services;
using ModelContextProtocol.Server;

namespace StreamDownloader.Tools;

[McpServerToolType]
public static class StreamDownloaderTools
{
    [McpServerTool(
        Name = "stream_capabilities",
        Title = "Inspect Stream-Downloader readiness",
        ReadOnly = true,
        Destructive = false,
        Idempotent = true,
        OpenWorld = false,
        UseStructuredContent = true)]
    [Description("Reports yt-dlp, FFmpeg and JavaScript-runtime readiness, quality presets, allowed roots, the state directory, and how this machine's processors, memory and disk sized download concurrency, without changing state.")]
    public static McpResponse<CapabilitiesData> GetCapabilities(StreamCoreService core) =>
        core.GetCapabilities();

    [McpServerTool(
        Name = "stream_download",
        Title = "Download a video from a URL in one call",
        ReadOnly = false,
        Destructive = false,
        Idempotent = true,
        OpenWorld = true,
        UseStructuredContent = true)]
    [Description("One-call download. Give a URL and where to save it and the video is downloaded to disk. Watch pages such as YouTube (and every other site yt-dlp supports) are resolved with yt-dlp and separate HD video+audio streams are merged with ffmpeg; the result is verified with ffprobe, hashed and moved into place without overwriting. Returns immediately with a job; poll stream_download_status or stream_download_wait for completion.")]
    public static Task<McpResponse<JobView>> DownloadAsync(
        StreamCoreService core,
        [Description("Absolute HTTP/HTTPS URL. A watch page (e.g. https://www.youtube.com/watch?v=...) or a direct media URL.")] string url,
        [Description("Where to save it: an absolute directory (C:\\Videos) OR an exact file path (C:\\Videos\\MyVideo01.mp4). If a full file path is given, its extension sets the format. Omit to use the default Downloads directory.")] string? destination = null,
        [Description("Optional filename to use when 'destination' is a directory. Ignored when 'destination' is already a full file path. An extension is added automatically if missing.")] string? fileName = null,
        [Description("Maximum video height: best, 2160p, 1440p, 1080p, 720p, 480p, 360p, 240p, 144p; or audio. Not an exact rendition guarantee.")] string quality = "best",
        [Description("Video container: mp4 (default), mkv, webm. Audio: mp3 (default), m4a, flac, wav, opus, aac. Filename extension overrides this. Unsupported codec/container combinations fail, not merely rename.")] string? format = null,
        [Description("Optional caller key (8-128 chars) so repeating the same call returns the same job. Omitted keys are derived deterministically from the request.")] string? idempotencyKey = null,
        CancellationToken cancellationToken = default) =>
        core.DownloadAsync(url, destination, fileName, quality, format, idempotencyKey, cancellationToken);

    [McpServerTool(
        Name = "stream_download_status",
        Title = "Get Stream-Downloader job status",
        ReadOnly = true,
        Destructive = false,
        Idempotent = true,
        OpenWorld = false,
        UseStructuredContent = true)]
    [Description("Returns a durable job state, transfer progress of the stream currently downloading, verification evidence, destination, failures, and recent state events. It never exposes the protected source URL.")]
    public static Task<McpResponse<JobView>> StatusAsync(
        StreamCoreService core,
        [Description("Stream-Downloader job ID.")] string jobId,
        CancellationToken cancellationToken = default) =>
        core.GetStatusAsync(jobId, cancellationToken);

    [McpServerTool(
        Name = "stream_download_list",
        Title = "List Stream-Downloader jobs",
        ReadOnly = true,
        Destructive = false,
        Idempotent = true,
        OpenWorld = false,
        UseStructuredContent = true)]
    [Description("Lists sanitized jobs created by this MCP, newest first.")]
    public static Task<McpResponse<JobListData>> ListAsync(
        StreamCoreService core,
        [Description("Optional exact state filter such as downloading or completed_verified.")] string? stateFilter = null,
        [Description("Maximum results, 1-100.")] int limit = 25,
        CancellationToken cancellationToken = default) =>
        core.ListAsync(stateFilter, limit, cancellationToken);

    [McpServerTool(
        Name = "stream_download_wait",
        Title = "Wait briefly for a Stream-Downloader job change",
        ReadOnly = true,
        Destructive = false,
        Idempotent = true,
        OpenWorld = false,
        UseStructuredContent = true)]
    [Description("Waits at most 45 seconds for a job's state version to advance. It does not keep one MCP call open for the entire download.")]
    public static Task<McpResponse<JobView>> WaitAsync(
        StreamCoreService core,
        [Description("Stream-Downloader job ID.")] string jobId,
        [Description("Last stateVersion already observed.")] long afterVersion,
        [Description("Bounded wait in seconds, 1-45.")] int timeoutSeconds = 30,
        CancellationToken cancellationToken = default) =>
        core.WaitAsync(jobId, afterVersion, timeoutSeconds, cancellationToken);

    [McpServerTool(
        Name = "stream_download_retry",
        Title = "Retry a definitively failed Stream-Downloader job",
        ReadOnly = false,
        Destructive = false,
        Idempotent = true,
        OpenWorld = true,
        UseStructuredContent = true)]
    [Description("Creates a new download only for a definitively failed terminal job using a new idempotency key. It refuses ambiguous or still-running outcomes.")]
    public static Task<McpResponse<JobView>> RetryAsync(
        StreamCoreService core,
        [Description("Failed Stream-Downloader job ID.")] string jobId,
        [Description("New caller-selected idempotency key, 8-128 characters.")] string newIdempotencyKey,
        CancellationToken cancellationToken = default) =>
        core.RetryAsync(jobId, newIdempotencyKey, cancellationToken);
}
