using System.Text.Json;
using StreamDownloader.Models;

namespace StreamDownloader.Services;

internal sealed class StateStore : IDisposable
{
    private static readonly JsonSerializerOptions JsonOptions = new()
    {
        PropertyNamingPolicy = JsonNamingPolicy.CamelCase,
        WriteIndented = true
    };

    private readonly string _stateFile;
    private readonly FileStream _ownership;
    private readonly SemaphoreSlim _gate = new(1, 1);
    private PersistentState _state;

    public StateStore(string stateDirectory)
    {
        Directory.CreateDirectory(stateDirectory);
        _stateFile = Path.Combine(stateDirectory, "jobs.json");
        try
        {
            _ownership = new FileStream(Path.Combine(stateDirectory, "owner.lock"), FileMode.OpenOrCreate,
                FileAccess.ReadWrite, FileShare.None);
        }
        catch (IOException ex)
        {
            throw new IOException("Another Stream-Downloader host owns this state directory. Close it before starting another host.", ex);
        }
        try { _state = Load(); }
        catch { _ownership.Dispose(); throw; }
    }

    public string StateFile => _stateFile;

    public async Task<T> ReadAsync<T>(Func<PersistentState, T> reader, CancellationToken cancellationToken)
    {
        await _gate.WaitAsync(cancellationToken).ConfigureAwait(false);
        try
        {
            return reader(Clone(_state));
        }
        finally
        {
            _gate.Release();
        }
    }

    public async Task<T> MutateAsync<T>(Func<PersistentState, T> mutation, CancellationToken cancellationToken)
    {
        await _gate.WaitAsync(cancellationToken).ConfigureAwait(false);
        try
        {
            var next = Clone(_state);
            var result = mutation(next);
            await SaveUnsafeAsync(next, cancellationToken).ConfigureAwait(false);
            _state = Clone(next);
            return result;
        }
        finally
        {
            _gate.Release();
        }
    }

    private PersistentState Load()
    {
        if (!File.Exists(_stateFile))
        {
            return new PersistentState();
        }

        using var document = JsonDocument.Parse(File.ReadAllText(_stateFile));
        var root = document.RootElement;
        if (root.ValueKind != JsonValueKind.Object ||
            new[] { "schemaVersion", "jobs", "idempotency" }
                .Any(name => !root.TryGetProperty(name, out _)))
            throw new InvalidDataException("jobs.json is incomplete; keep it and restore a verified backup deliberately.");
        var state = root.Deserialize<PersistentState>(JsonOptions)
            ?? throw new InvalidDataException("jobs.json contains null; keep it and restore a verified backup deliberately.");
        if (state.SchemaVersion != 1 || state.Jobs is null || state.Idempotency is null ||
            state.Jobs.Any(pair => pair.Value is null || pair.Value.JobId != pair.Key) ||
            state.Idempotency.Any(pair => pair.Value is null || !state.Jobs.TryGetValue(pair.Value, out var job) || job.IdempotencyKey != pair.Key))
            throw new InvalidDataException("Unsupported or inconsistent jobs.json; keep it and restore a verified backup deliberately.");
        return state;
    }

    private async Task SaveUnsafeAsync(PersistentState state, CancellationToken cancellationToken)
    {
        var temp = _stateFile + ".tmp";
        await using (var stream = new FileStream(
            temp,
            FileMode.Create,
            FileAccess.Write,
            FileShare.None,
            65536,
            FileOptions.WriteThrough | FileOptions.Asynchronous))
        {
            await JsonSerializer.SerializeAsync(stream, state, JsonOptions, cancellationToken).ConfigureAwait(false);
            await stream.FlushAsync(cancellationToken).ConfigureAwait(false);
            stream.Flush(flushToDisk: true);
        }

        cancellationToken.ThrowIfCancellationRequested();
        if (File.Exists(_stateFile)) File.Replace(temp, _stateFile, _stateFile + ".bak");
        else File.Move(temp, _stateFile);
    }

    private static PersistentState Clone(PersistentState state) =>
        JsonSerializer.Deserialize<PersistentState>(JsonSerializer.SerializeToUtf8Bytes(state, JsonOptions), JsonOptions)!;

    public void Dispose() => _ownership.Dispose();
}
