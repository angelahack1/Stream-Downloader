using System.Text.Json.Serialization;

namespace StreamDownloader.Models;

public sealed record McpResponse<T>(
    bool Ok,
    string Code,
    string Message,
    T? Data,
    DateTimeOffset ServerTime)
{
    public static McpResponse<T> Success(string code, string message, T data) =>
        new(true, code, message, data, DateTimeOffset.UtcNow);

    public static McpResponse<T> Failure(string code, string message, T? data = default) =>
        new(false, code, message, data, DateTimeOffset.UtcNow);
}

public sealed record CapabilitiesData(
    string ServerName,
    string ServerVersion,
    string Runtime,
    string OperatingSystem,
    SiteExtractionCapability SiteExtraction,
    MachineCapability Machine,
    IReadOnlyList<string> Tools,
    IReadOnlyList<string> AllowedRoots,
    string StateDirectory);

public sealed record SiteExtractionCapability(
    bool YtDlpAvailable,
    string? YtDlpPath,
    string? YtDlpVersion,
    bool FfmpegAvailable,
    string? FfmpegPath,
    IReadOnlyList<string> QualityPresets,
    string? JavaScriptRuntime = null);

public sealed record MachineCapability(
    int LogicalProcessors,
    long MemoryBytes,
    string DefaultVolumeStorage,
    string Summary,
    int MaxConcurrentDownloads,
    int ConcurrentFragments,
    string ChildPriority,
    string Reason);

public sealed record JobView(
    string JobId,
    string State,
    bool Terminal,
    string Destination,
    string? StagingDirectory,
    string? SourceHost,
    string? SourceTitle,
    string? Quality,
    string? Container,
    long? ObservedBytes,
    double? StreamPercent,
    string? ObservedSha256,
    string? FailureCode,
    string? FailureMessage,
    DateTimeOffset CreatedAt,
    DateTimeOffset UpdatedAt,
    long StateVersion,
    IReadOnlyList<JobEvent> Events,
    MediaVerification? Media = null);

public sealed record MediaVerification(string Container, int? Height, bool HasAudio, double DurationSeconds);

public sealed record JobEvent(
    DateTimeOffset At,
    string State,
    string Code,
    string Message);

public sealed record JobListData(
    IReadOnlyList<JobView> Jobs,
    int Total);

/// <summary>Resolved download destination: directory, optional exact filename, and container/format.</summary>
internal sealed record DownloadTarget(string Directory, string? FileName, string? Container);

internal sealed class PersistentState
{
    public int SchemaVersion { get; set; } = 1;
    public Dictionary<string, JobRecord> Jobs { get; set; } = new(StringComparer.Ordinal);
    public Dictionary<string, string> Idempotency { get; set; } = new(StringComparer.Ordinal);
}

internal sealed class JobRecord
{
    public required string JobId { get; set; }
    public required string IdempotencyKey { get; set; }
    public required string ProtectedUrl { get; set; }
    public required string DestinationDirectory { get; set; }
    public required string FileName { get; set; }
    public required string StagingDirectory { get; set; }
    public required string State { get; set; }

    public string? SourcePageHost { get; set; }
    public string? SourceTitle { get; set; }
    public string? Quality { get; set; }
    public string? Container { get; set; }

    public string? RequestFingerprint { get; set; }
    public string? FinalizingSource { get; set; }
    public MediaVerification? Media { get; set; }
    public long? ObservedBytes { get; set; }
    public double? StreamPercent { get; set; }
    public string? ObservedSha256 { get; set; }
    public string? FailureCode { get; set; }
    public string? FailureMessage { get; set; }
    public DateTimeOffset CreatedAt { get; set; }
    public DateTimeOffset UpdatedAt { get; set; }
    public long StateVersion { get; set; }
    public List<JobEvent> Events { get; set; } = [];

    [JsonIgnore]
    public string FinalPath => Path.Combine(DestinationDirectory, FileName);
}
