using System.Diagnostics;
using System.Net;
using System.Text.Json;
using System.Security.Cryptography;
using StreamDownloader.Models;
using StreamDownloader.Services;

if (args.FirstOrDefault() == "--fixture")
{
    if (args.ElementAtOrDefault(1) == "sleep") await Task.Delay(TimeSpan.FromMinutes(1));
    if (args.ElementAtOrDefault(1) == "stdin")
    {
        Console.WriteLine((await Console.In.ReadToEndAsync()).Length == 0 ? "stdin closed" : "unexpected input");
        return 0;
    }
    if (args.ElementAtOrDefault(1) == "priority")
    {
        // Give the parent time to lower the class, then report what the OS actually applied.
        await Task.Delay(700);
        using var self = Process.GetCurrentProcess();
        Console.WriteLine("priority " + self.PriorityClass);
        return 0;
    }
    if (args.ElementAtOrDefault(1) == "priority-child")
    {
        // Launch a grandchild the way yt-dlp launches FFmpeg and relay the class it inherited.
        await Task.Delay(700);
        using var child = Process.Start(new ProcessStartInfo(Environment.ProcessPath!, "--fixture priority")
        {
            RedirectStandardOutput = true,
            UseShellExecute = false,
            CreateNoWindow = true
        })!;
        Console.WriteLine("child " + (await child.StandardOutput.ReadToEndAsync()).Trim());
        await child.WaitForExitAsync();
        return 0;
    }
    Console.WriteLine("fixture stdout");
    Console.Error.WriteLine("fixture stderr");
    return 0;
}

var testRoot = args.Length == 2 && args[0] == "--output" ? Path.GetFullPath(args[1]) :
    Path.Combine(Path.GetTempPath(), "Stream-Downloader-tests-" + Guid.NewGuid().ToString("N"));
if (Directory.Exists(testRoot) && Directory.EnumerateFileSystemEntries(testRoot).Any())
    throw new InvalidOperationException("Unit test output must be a new or empty directory.");
Directory.CreateDirectory(testRoot);
var liveLog = Path.Combine(testRoot, "live.log");
var results = new List<object>();
var failed = 0;
async Task Test(string name, Func<Task> action)
{
    var started = Stopwatch.StartNew();
    string line;
    try
    {
        await action();
        results.Add(new { name, passed = true, milliseconds = started.ElapsedMilliseconds });
        Console.ForegroundColor = ConsoleColor.Green;
        line = "PASS " + name;
    }
    catch (Exception ex)
    {
        failed++;
        results.Add(new { name, passed = false, error = ex.ToString(), milliseconds = started.ElapsedMilliseconds });
        Console.ForegroundColor = ConsoleColor.Red;
        line = "FAIL " + name + ": " + ex.Message;
    }
    Console.WriteLine(line);
    Console.ResetColor();
    File.AppendAllText(liveLog, DateTime.Now.ToString("HH:mm:ss ") + line + Environment.NewLine);
}
void Check(bool condition, string? because = null) { if (!condition) throw new Exception("Assertion failed" + (because is null ? "" : ": " + because)); }
void Reject(Action action) { try { action(); } catch (Exception) { return; } throw new Exception("Expected rejection"); }
Console.ForegroundColor = ConsoleColor.Cyan;
Console.WriteLine("Stream-Downloader unit/regression harness - Angela, every check prints as it runs.");
Console.ResetColor();

// ---------- Paths (mirrors the original harness) ----------
var paths = new PathPolicy([testRoot], true);
foreach (var name in new[] { "CON.txt", "CON.foo.bar", "NUL", "a.txt ", "a.", "a:b", "../escape", "sub\\a", "LPT9.json", "" })
    await Test("reject filename " + name, () => { Reject(() => paths.ValidateFileName(name)); return Task.CompletedTask; });
await Test("normal filename", () => { Check(paths.ValidateFileName("Video 01.mp4") == "Video 01.mp4"); return Task.CompletedTask; });
await Test("root boundary", () => { Reject(() => paths.ValidateDirectory(testRoot + "-sibling", testRoot)); return Task.CompletedTask; });
await Test("UNC rejection", () => { Reject(() => paths.ValidateDirectory(@"\\server\share", testRoot)); return Task.CompletedTask; });
await Test("drive root preserved", () => { Check(new PathPolicy([], false).ValidateDirectory(@"C:\", testRoot) == @"C:\"); return Task.CompletedTask; });

// ---------- Network policy ----------
foreach (var ip in new[] { "127.0.0.1", "10.1.2.3", "::1", "::ffff:192.168.1.5", "::ffff:127.0.0.1", "fc00::1", "169.254.1.1", "100.64.0.1" })
    await Test("private address " + ip, () => { Check(NetworkInspector.IsPrivateOrSpecial(IPAddress.Parse(ip))); return Task.CompletedTask; });
await Test("public address", () => { Check(!NetworkInspector.IsPrivateOrSpecial(IPAddress.Parse("1.1.1.1"))); return Task.CompletedTask; });

// ---------- Format selection ----------
foreach (var quality in YtDlpService.QualityPresets.Where(q => q.EndsWith('p')))
    await Test("height cap every fallback " + quality, () => { Check(YtDlpService.BuildVideoFormat(quality, "mp4").Split('/').All(s => s.Contains("[height<=?" + quality.TrimEnd('p') + "]"))); return Task.CompletedTask; });
await Test("invalid quality", () => { Reject(() => YtDlpService.BuildVideoFormat("999p", "mp4")); return Task.CompletedTask; });
await Test("best mp4 prefers H.264/AAC pairing first", () => { Check(YtDlpService.BuildVideoFormat("best", null) == "bv*[ext=mp4]+ba[ext=m4a]/bv*+ba/b"); return Task.CompletedTask; });
foreach (var container in new[] { "mkv", "webm" })
    await Test("non-mp4 container never forces mp4 streams " + container, () => { Check(YtDlpService.BuildVideoFormat("720p", container) == "bv*[height<=?720]+ba/b[height<=?720]"); return Task.CompletedTask; });

// ---------- Progress parsing (feeds the live progress Angela watches) ----------
await Test("progress line parsed", () => {
    var progress = YtDlpService.ParseProgressLine("@@STREAMDL_PROGRESS@@  45.2%|1048576|2097152|NA");
    Check(progress is not null && progress.Percent == 45.2 && progress.DownloadedBytes == 1048576 && progress.TotalBytes == 2097152);
    return Task.CompletedTask; });
await Test("progress falls back to the estimate", () => {
    var progress = YtDlpService.ParseProgressLine("@@STREAMDL_PROGRESS@@ 3.0%|90000|NA|3000000");
    Check(progress is not null && progress.TotalBytes == 3000000);
    return Task.CompletedTask; });
await Test("progress tolerates unknown fields", () => {
    var progress = YtDlpService.ParseProgressLine("@@STREAMDL_PROGRESS@@ NA|NA|NA|NA");
    Check(progress is not null && progress.Percent is null && progress.DownloadedBytes is null && progress.TotalBytes is null);
    return Task.CompletedTask; });
await Test("non-progress output ignored", () => { Check(YtDlpService.ParseProgressLine("[download] Destination: x.mp4") is null && YtDlpService.ParseProgressLine("@@STREAMDL_PROGRESS@@ 1%|2") is null); return Task.CompletedTask; });

// ---------- State store (mirrors the original harness) ----------
var stateDirectory = Path.Combine(testRoot, "state");
using (var store = new StateStore(stateDirectory))
{
    await Test("single state owner", () => { Reject(() => { using var second = new StateStore(stateDirectory); }); return Task.CompletedTask; });
    await Test("snapshot isolation", async () => { var leaked = await store.ReadAsync(s => s, default); leaked.SchemaVersion = 88; Check(await store.ReadAsync(s => s.SchemaVersion, default) == 1); });
    await Test("mutation exception rollback", async () => {
        try { await store.MutateAsync<bool>(s => { s.SchemaVersion = 99; throw new IOException("injected"); }, default); } catch (IOException) { }
        Check(await store.ReadAsync(s => s.SchemaVersion, default) == 1);
    });
    await Test("commit and previous-state backup", async () => {
        await store.MutateAsync(s => true, default); await store.MutateAsync(s => true, default);
        Check(File.Exists(store.StateFile + ".bak"));
    });
    await Test("failed persistence rollback", async () => {
        Directory.CreateDirectory(store.StateFile + ".tmp");
        try { await store.MutateAsync(s => { s.SchemaVersion = 88; return true; }, default); throw new Exception("Expected disk failure"); }
        catch (UnauthorizedAccessException) { }
        Check(await store.ReadAsync(s => s.SchemaVersion, default) == 1);
        Directory.Delete(store.StateFile + ".tmp");
    });
    await Test("concurrent readers writers", async () => {
        await Task.WhenAll(Enumerable.Range(0, 20).Select(_ => store.MutateAsync(s => true, default)));
        Check(await store.ReadAsync(s => s.SchemaVersion, default) == 1);
    });
}
await Test("state reopens", () => { using var store = new StateStore(stateDirectory); return Task.CompletedTask; });
await Test("corrupt state fails closed", () => {
    File.WriteAllText(Path.Combine(stateDirectory, "jobs.json"), "{broken");
    Reject(() => { using var store = new StateStore(stateDirectory); });
    Check(File.ReadAllText(Path.Combine(stateDirectory, "jobs.json")) == "{broken"); return Task.CompletedTask;
});
await Test("DPAPI roundtrip", () => { var value = "https://example.test/?secret=private"; var protectedValue = SecretVault.Protect(value); Check(!protectedValue.Contains("secret")); Check(SecretVault.Unprotect(protectedValue) == value); return Task.CompletedTask; });
var emptyState = JsonSerializer.Serialize(new PersistentState(), new JsonSerializerOptions { PropertyNamingPolicy = JsonNamingPolicy.CamelCase });
foreach (var (name, json) in new[] {
    ("empty object", "{}"), ("null root", "null"), ("array root", "[]"),
    ("missing section", emptyState.Replace("\"jobs\":{},", "")),
    ("unsupported schema", emptyState.Replace("\"schemaVersion\":1", "\"schemaVersion\":2")),
    ("null jobs", emptyState.Replace("\"jobs\":{}", "\"jobs\":null")),
    ("null job record", emptyState.Replace("\"jobs\":{}", "\"jobs\":{\"bad\":null}")),
    ("orphan idempotency", emptyState.Replace("\"idempotency\":{}", "\"idempotency\":{\"key\":\"missing\"}")) })
    await Test("invalid state " + name, () => {
        Check(name is "empty object" or "null root" or "array root" || json != emptyState, "the fault was not injected");
        File.WriteAllText(Path.Combine(stateDirectory, "jobs.json"), json);
        Reject(() => { using var store = new StateStore(stateDirectory); });
        Check(File.ReadAllText(Path.Combine(stateDirectory, "jobs.json")) == json);
        return Task.CompletedTask;
    });

// ---------- Acquisition checksum (mirrors the original harness) ----------
var toolFile = Path.Combine(testRoot, "checksum-fixture.bin");
File.WriteAllText(toolFile, "checksum fixture, never executed");
var toolHash = Convert.ToHexString(SHA256.HashData(File.ReadAllBytes(toolFile)));
await Test("checksum valid GNU format", () => YtDlpService.VerifyChecksumAsync(toolFile, toolHash.ToLowerInvariant() + "  yt-dlp.exe\r\n", default));
await Test("checksum valid binary marker", () => YtDlpService.VerifyChecksumAsync(toolFile, toolHash + " *yt-dlp.exe\n", default));
foreach (var (name, document) in new[] { ("mismatch", new string('0',64) + "  yt-dlp.exe"),
    ("missing entry", toolHash + "  other.exe"), ("duplicate entry", toolHash + "  yt-dlp.exe\n" + toolHash + "  yt-dlp.exe") })
    await Test("checksum rejects " + name, async () => {
        try { await YtDlpService.VerifyChecksumAsync(toolFile, document, default); }
        catch (YtDlpException) { return; }
        throw new Exception("Bad checksum accepted");
    });

// ---------- Child processes (mirrors the original harness) ----------
var testExecutable = Environment.ProcessPath!;
await Test("process output collected", async () => {
    var lines = new List<string>();
    var code = await YtDlpService.RunProcessAsync(testExecutable, ["--fixture", "emit"], lines.Add, _ => { }, TimeSpan.FromSeconds(10), default);
    Check(code == 0 && lines.Contains("fixture stdout"));
});
await Test("child input is closed and separate from transport", async () => {
    var lines = new List<string>();
    var code = await YtDlpService.RunProcessAsync(testExecutable, ["--fixture", "stdin"], lines.Add, _ => { }, TimeSpan.FromSeconds(10), default);
    Check(code == 0 && lines.Contains("stdin closed"));
});
foreach (var stream in new[] { "stdout", "stderr" })
    await Test("malformed " + stream + " cannot crash host", async () => {
        try {
            await YtDlpService.RunProcessAsync(testExecutable, ["--fixture", "emit"],
                _ => { if (stream == "stdout") throw new JsonException("injected"); },
                _ => { if (stream == "stderr") throw new JsonException("injected"); }, TimeSpan.FromSeconds(10), default);
        } catch (YtDlpException) { return; }
        throw new Exception("Callback error not surfaced");
    });
await Test("process timeout terminates child", async () => {
    try { await YtDlpService.RunProcessAsync(testExecutable, ["--fixture", "sleep"], _ => { }, _ => { }, TimeSpan.FromMilliseconds(500), default); }
    catch (YtDlpException ex) when (ex.Message.Contains("time budget")) { return; }
    throw new Exception("Timeout not surfaced");
});
await Test("process cancellation terminates child", async () => {
    using var cancellation = new CancellationTokenSource(TimeSpan.FromMilliseconds(500));
    try { await YtDlpService.RunProcessAsync(testExecutable, ["--fixture", "sleep"], _ => { }, _ => { }, TimeSpan.FromSeconds(10), cancellation.Token); }
    catch (OperationCanceledException) { return; }
    throw new Exception("Cancellation not surfaced");
});
await Test("lowered child priority is really applied by Windows", async () => {
    var lines = new List<string>();
    var code = await YtDlpService.RunProcessAsync(testExecutable, ["--fixture", "priority"], lines.Add, _ => { },
        TimeSpan.FromSeconds(20), ProcessPriorityClass.BelowNormal, default);
    Check(code == 0 && lines.Contains("priority BelowNormal"), string.Join(" / ", lines));
});
await Test("grandchild (like FFmpeg under yt-dlp) inherits the lowered priority", async () => {
    var lines = new List<string>();
    var code = await YtDlpService.RunProcessAsync(testExecutable, ["--fixture", "priority-child"], lines.Add, _ => { },
        TimeSpan.FromSeconds(20), ProcessPriorityClass.BelowNormal, default);
    Check(code == 0 && lines.Contains("child priority BelowNormal"), string.Join(" / ", lines));
});
await Test("normal priority request leaves the child untouched", async () => {
    var lines = new List<string>();
    var code = await YtDlpService.RunProcessAsync(testExecutable, ["--fixture", "priority"], lines.Add, _ => { },
        TimeSpan.FromSeconds(20), ProcessPriorityClass.Normal, default);
    Check(code == 0 && lines.Contains("priority Normal"), string.Join(" / ", lines));
});

// ---------- Hardware the developer does not have, injected ----------
const long GiB = 1024L * 1024 * 1024;
var workstation = new MachineFacts(22, 16 * GiB, StorageKind.Nvme, "C: is on an NVMe bus.");
var laptopHdd = new MachineFacts(4, 8 * GiB, StorageKind.Hdd, "D: reports a seek penalty: mechanical disk.");
var laptopSsd = new MachineFacts(4, 8 * GiB, StorageKind.Ssd, "C: reports no seek penalty: solid-state.");
var lowMemory = new MachineFacts(12, 6 * GiB, StorageKind.Ssd, "C: reports no seek penalty: solid-state.");
var unknownDisk = new MachineFacts(8, 16 * GiB, StorageKind.Unknown, "C: would not answer the seek-penalty query.");
var cardReader = new MachineFacts(16, 32 * GiB, StorageKind.Removable, "E: is on a USB or card-reader bus.");
var memoryUnknown = new MachineFacts(16, 0, StorageKind.Ssd, "C: reports no seek penalty: solid-state.");
await Test("hardware: 4 cores, 8 GiB, mechanical disk -> 1 fragment, BelowNormal, 1 slot", () => {
    var plan = MachineProfile.PlanDownload(laptopHdd, 0, "auto");
    var slots = MachineProfile.PlanSlots(laptopHdd, 0);
    Check(plan.ConcurrentFragments == 1 && plan.ChildPriority == ProcessPriorityClass.BelowNormal && plan.Reason.Contains("seek penalty"), plan.Reason);
    Check(slots.Slots == 1 && slots.Reason.Contains("seek penalty"), slots.Reason);
    return Task.CompletedTask; });
await Test("hardware: 4 cores on SSD -> 2 fragments, 1 slot, the reason names the processors", () => {
    var plan = MachineProfile.PlanDownload(laptopSsd, 0, "auto");
    Check(plan.ConcurrentFragments == 2 && plan.Reason.Contains("4 logical processors"), plan.Reason);
    Check(MachineProfile.PlanSlots(laptopSsd, 0).Slots == 1);
    return Task.CompletedTask; });
await Test("hardware: 6 GiB of memory -> 2 fragments, 1 slot, the reason names the memory", () => {
    var plan = MachineProfile.PlanDownload(lowMemory, 0, "auto");
    var slots = MachineProfile.PlanSlots(lowMemory, 0);
    Check(plan.ConcurrentFragments == 2 && plan.Reason.Contains("6 GiB"), plan.Reason);
    Check(slots.Slots == 1 && slots.Reason.Contains("6 GiB"), slots.Reason);
    return Task.CompletedTask; });
await Test("hardware: workstation keeps the ceiling and Normal priority", () => {
    var plan = MachineProfile.PlanDownload(workstation, 0, "auto");
    var slots = MachineProfile.PlanSlots(workstation, 0);
    Check(plan.ConcurrentFragments == 4 && plan.ChildPriority == ProcessPriorityClass.Normal, plan.Reason);
    Check(slots.Slots == 4, slots.Reason);
    return Task.CompletedTask; });
await Test("hardware: unknown storage is not treated as the worst case", () => {
    var plan = MachineProfile.PlanDownload(unknownDisk, 0, "auto");
    Check(plan.ConcurrentFragments == 4 && plan.ChildPriority == ProcessPriorityClass.BelowNormal, plan.Reason);
    Check(MachineProfile.PlanSlots(unknownDisk, 0).Slots == 2);
    return Task.CompletedTask; });
await Test("hardware: removable media is seek-bound even on a big machine", () => {
    Check(MachineProfile.PlanDownload(cardReader, 0, "auto").ConcurrentFragments == 1 && MachineProfile.PlanSlots(cardReader, 0).Slots == 1);
    return Task.CompletedTask; });
await Test("hardware: unknown memory is not low memory", () => {
    Check(!memoryUnknown.LowMemory && MachineProfile.PlanDownload(memoryUnknown, 0, "auto").ConcurrentFragments == 4 && MachineProfile.PlanSlots(memoryUnknown, 0).Slots == 4);
    return Task.CompletedTask; });
await Test("hardware: explicit settings are obeyed exactly on a small machine", () => {
    var plan = MachineProfile.PlanDownload(laptopHdd, 8, "normal");
    var slots = MachineProfile.PlanSlots(laptopHdd, 3);
    Check(plan.ConcurrentFragments == 8 && plan.ChildPriority == ProcessPriorityClass.Normal && plan.Reason.Contains("obeyed exactly"), plan.Reason);
    Check(slots.Slots == 3 && slots.Reason.Contains("obeyed exactly"), slots.Reason);
    Check(MachineProfile.PlanDownload(workstation, 0, "below_normal").ChildPriority == ProcessPriorityClass.BelowNormal);
    return Task.CompletedTask; });
await Test("hardware: priority is never raised above Normal", () => {
    foreach (var requested in new[] { "high", "realtime", "abovenormal", "", "AUTO" })
    {
        var plan = MachineProfile.PlanDownload(workstation, 0, requested);
        Check(plan.ChildPriority is ProcessPriorityClass.Normal or ProcessPriorityClass.BelowNormal, requested);
    }
    return Task.CompletedTask; });
await Test("hardware: the live probe answers and never throws", () => {
    var here = MachineProfile.Probe(testRoot);
    Check(here.LogicalProcessors >= 1 && here.Summary.Length > 0 && here.StorageDetail.Length > 0, here.Summary);
    Check(MachineProfile.Probe(@"\\server\share\videos").Storage == StorageKind.Network);
    var missing = MachineProfile.Probe(@"Q:\no-such-volume\x");
    Check(missing.LogicalProcessors >= 1 && missing.StorageDetail.Length > 0);
    Console.WriteLine("     this machine: " + here.Summary + " -> " + MachineProfile.PlanDownload(here, 0, "auto").Reason);
    return Task.CompletedTask; });

// ---------- Own identity (decoupled defaults) ----------
await Test("defaults use Stream-Downloader's own state and tools directories", () => {
    var options = new StreamDownloaderOptions();
    Check(options.StateDirectory == @"%LOCALAPPDATA%\Stream-Downloader\state" && options.ToolsDirectory == @"%LOCALAPPDATA%\Stream-Downloader\tools");
    Check(options.ConcurrentFragments == 0 && options.MaxConcurrentDownloads == 0 && options.ChildProcessPriority == "auto");
    return Task.CompletedTask; });

Console.ForegroundColor = failed == 0 ? ConsoleColor.Green : ConsoleColor.Red;
var verdict = $"{results.Count - failed}/{results.Count} passed; evidence: {testRoot}";
Console.WriteLine(verdict);
Console.ResetColor();
File.AppendAllText(liveLog, DateTime.Now.ToString("HH:mm:ss ") + verdict + Environment.NewLine);
File.WriteAllText(Path.Combine(testRoot, "results.json"), JsonSerializer.Serialize(results, new JsonSerializerOptions { WriteIndented = true }));
return failed == 0 ? 0 : 1;
