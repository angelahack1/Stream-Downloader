using System.Collections.Concurrent;
using System.Diagnostics;
using System.Globalization;
using System.Runtime.InteropServices;
using Microsoft.Win32.SafeHandles;

namespace StreamDownloader.Services;

internal enum StorageKind
{
    Unknown,
    Nvme,
    Ssd,
    Hdd,
    Removable,
    Network
}

/// <summary>
/// What this machine is, as it relates to one volume. Every field has a safe answer when unknown.
/// </summary>
internal sealed record MachineFacts(int LogicalProcessors, long MemoryBytes, StorageKind Storage, string StorageDetail)
{
    /// <summary>Below this, simultaneous downloads and merges are what run the machine out of memory.</summary>
    public const long LowMemoryBytes = 8L * 1024 * 1024 * 1024;

    /// <summary>At or below this many logical processors, child tools give the desktop a core back.</summary>
    public const int SmallCpuCeiling = 8;

    /// <summary>
    /// True when concurrent writers cost more than they buy: a mechanical disk has one head, a card
    /// reader one slow controller. Unknown storage is deliberately NOT seek-bound: assuming a spindle
    /// would throttle every machine whose disk could not be identified, while the processor and
    /// memory guards below still protect that case.
    /// </summary>
    public bool SeekBound => Storage is StorageKind.Hdd or StorageKind.Removable;

    /// <summary>Unknown memory (0) is not low memory.</summary>
    public bool LowMemory => MemoryBytes > 0 && MemoryBytes < LowMemoryBytes;

    public bool Modest => SeekBound || LogicalProcessors <= 4 || LowMemory;

    public string Memory => MemoryBytes > 0
        ? string.Create(CultureInfo.InvariantCulture, $"{MemoryBytes / (1024d * 1024 * 1024):0.#} GiB RAM")
        : "memory unknown";

    public string Summary => $"{LogicalProcessors} logical processors, {Memory}, {MachineProfile.Label(Storage)}";
}

/// <summary>What one download may ask of this machine, and the measurement that decided it.</summary>
internal sealed record DownloadResources(int ConcurrentFragments, ProcessPriorityClass ChildPriority, string Reason);

/// <summary>
/// Sizes downloads from the machine they run on, not from the workstation they were written on.
/// A fixed four concurrent fragments, unlimited simultaneous jobs and Normal-priority children are
/// right for a 22-thread NVMe workstation and hostile on a four-core laptop with a 7200 rpm disk:
/// the spindle has one head, so concurrent writers go slower than one, and a desktop whose cores are
/// all busy stops repainting. This answers four questions (processors, memory, what kind of volume,
/// removable or remote); every answer degrades to a safe "could not tell" instead of throwing,
/// because a hardware probe must never stop a download. Explicit settings are always obeyed exactly.
/// Ported from Angela López Mendoza's reference implementation in Lumen-Book-Reader.
/// </summary>
internal static class MachineProfile
{
    private const uint IoctlStorageQueryProperty = 0x002D1400;
    private const int StorageDeviceProperty = 0;
    private const int StorageDeviceSeekPenaltyProperty = 7;
    private const uint FileShareReadWrite = 0x00000001 | 0x00000002;
    private const uint OpenExisting = 3;
    private const int BusTypeUsb = 0x07;
    private const int BusTypeSd = 0x0C;
    private const int BusTypeMmc = 0x0D;
    private const int BusTypeNvme = 0x11;

    private static readonly ConcurrentDictionary<string, MachineFacts> Cache = new(StringComparer.OrdinalIgnoreCase);

    public static string Label(StorageKind storage) => storage switch
    {
        StorageKind.Nvme => "NVMe solid-state",
        StorageKind.Ssd => "SATA solid-state",
        StorageKind.Hdd => "mechanical disk (seek penalty)",
        StorageKind.Removable => "removable media",
        StorageKind.Network => "network drive",
        _ => "unknown storage"
    };

    /// <summary>This machine, as it relates to the volume holding <paramref name="path"/>. Never throws.</summary>
    public static MachineFacts Probe(string path)
    {
        string root;
        try
        {
            root = Path.GetPathRoot(Path.GetFullPath(path)) ?? path;
        }
        catch (Exception)
        {
            root = path;
        }

        return Cache.GetOrAdd(root, key =>
        {
            var (storage, detail) = DetectStorage(key);
            return new MachineFacts(Math.Max(1, Environment.ProcessorCount), TotalMemoryBytes(), storage, detail);
        });
    }

    /// <summary>Fragments and child priority for one download. Pure: tests inject any machine.</summary>
    public static DownloadResources PlanDownload(MachineFacts machine, int configuredFragments, string? configuredPriority)
    {
        int fragments;
        string fragmentReason;
        if (configuredFragments > 0)
        {
            fragments = configuredFragments;
            fragmentReason = $"ConcurrentFragments={configuredFragments} is configured and obeyed exactly";
        }
        else if (machine.SeekBound)
        {
            fragments = 1;
            fragmentReason = $"1 concurrent fragment because {machine.StorageDetail.TrimEnd('.')}";
        }
        else if (machine.LogicalProcessors <= 4)
        {
            fragments = 2;
            fragmentReason = $"2 concurrent fragments because there are only {machine.LogicalProcessors} logical processors";
        }
        else if (machine.LowMemory)
        {
            fragments = 2;
            fragmentReason = $"2 concurrent fragments because there is only {machine.Memory}";
        }
        else
        {
            fragments = 4;
            fragmentReason = $"4 concurrent fragments (the ceiling) for {machine.Summary}";
        }

        ProcessPriorityClass priority;
        string priorityReason;
        switch ((configuredPriority ?? "auto").Trim().ToLowerInvariant())
        {
            case "normal":
                priority = ProcessPriorityClass.Normal;
                priorityReason = "ChildProcessPriority=normal is configured and obeyed exactly";
                break;
            case "below_normal":
                priority = ProcessPriorityClass.BelowNormal;
                priorityReason = "ChildProcessPriority=below_normal is configured and obeyed exactly";
                break;
            default:
                var unrecognized = configuredPriority is not null &&
                    !configuredPriority.Trim().Equals("auto", StringComparison.OrdinalIgnoreCase) &&
                    configuredPriority.Trim().Length > 0
                        ? $" (unrecognized ChildProcessPriority '{configuredPriority}' treated as auto; never above Normal)"
                        : string.Empty;
                if (machine.LogicalProcessors <= MachineFacts.SmallCpuCeiling)
                {
                    priority = ProcessPriorityClass.BelowNormal;
                    priorityReason = $"BelowNormal priority so yt-dlp and FFmpeg leave the desktop a core on {machine.LogicalProcessors} logical processors{unrecognized}";
                }
                else
                {
                    priority = ProcessPriorityClass.Normal;
                    priorityReason = $"Normal priority because {machine.LogicalProcessors} logical processors leave the desktop room{unrecognized}";
                }
                break;
        }

        return new DownloadResources(fragments, priority, $"{fragmentReason}; {priorityReason}.");
    }

    /// <summary>How many downloads may run at once. Pure: tests inject any machine.</summary>
    public static (int Slots, string Reason) PlanSlots(MachineFacts machine, int configuredSlots)
    {
        if (configuredSlots > 0)
        {
            return (configuredSlots, $"MaxConcurrentDownloads={configuredSlots} is configured and obeyed exactly.");
        }

        if (machine.SeekBound)
        {
            return (1, $"1 download at a time because {machine.StorageDetail.TrimEnd('.')}.");
        }

        if (machine.LogicalProcessors <= 4)
        {
            return (1, $"1 download at a time because there are only {machine.LogicalProcessors} logical processors.");
        }

        if (machine.LowMemory)
        {
            return (1, $"1 download at a time because there is only {machine.Memory}.");
        }

        var byProcessors = Math.Max(2, machine.LogicalProcessors / 4);
        var byMemory = machine.MemoryBytes > 0 ? (int)Math.Max(1, machine.MemoryBytes / (4L * 1024 * 1024 * 1024)) : 4;
        var slots = Math.Min(4, Math.Min(byProcessors, byMemory));
        return (slots, $"{slots} downloads at a time for {machine.Summary}: at most one per 4 logical processors and per 4 GiB of memory, never more than 4.");
    }

    internal static (StorageKind Storage, string Detail) DetectStorage(string root)
    {
        try
        {
            if (string.IsNullOrEmpty(root) || root.StartsWith(@"\\", StringComparison.Ordinal))
            {
                return (StorageKind.Network, "the path is a UNC share with no local volume to query.");
            }

            var drive = root.TrimEnd('\\', '/');
            DriveType driveType;
            try
            {
                driveType = new DriveInfo(root).DriveType;
            }
            catch (Exception)
            {
                driveType = DriveType.Unknown;
            }

            if (driveType == DriveType.Network)
            {
                return (StorageKind.Network, $"{drive} is a mapped network drive.");
            }

            var (bus, seekPenalty) = QueryVolume(drive);
            if (bus == BusTypeNvme)
            {
                return (StorageKind.Nvme, $"{drive} is on an NVMe bus.");
            }

            if (bus is BusTypeUsb or BusTypeSd or BusTypeMmc)
            {
                return (StorageKind.Removable, $"{drive} is on a USB or card-reader bus; treated as seek-bound whatever the media claims.");
            }

            if (seekPenalty == true)
            {
                return (StorageKind.Hdd, $"{drive} reports a seek penalty: mechanical disk.");
            }

            if (seekPenalty == false)
            {
                return (StorageKind.Ssd, $"{drive} reports no seek penalty: solid-state.");
            }

            if (driveType == DriveType.Removable)
            {
                return (StorageKind.Removable, $"{drive} is removable media.");
            }

            return (StorageKind.Unknown, $"{drive} would not answer the seek-penalty query; assuming solid-state behaviour but leaving the processor and memory guards on.");
        }
        catch (Exception ex)
        {
            return (StorageKind.Unknown, $"the storage probe failed ({ex.GetType().Name}); leaving the processor and memory guards on.");
        }
    }

    private static (int? Bus, bool? SeekPenalty) QueryVolume(string drive)
    {
        // Zero desired access: enough for an informational IOCTL, and the only form of this call an
        // ordinary user may make (GENERIC_READ on \\.\C: needs Administrator).
        using var handle = CreateFileW(@"\\.\" + drive, 0, FileShareReadWrite, IntPtr.Zero, OpenExisting, 0, IntPtr.Zero);
        if (handle.IsInvalid)
        {
            return (null, null);
        }

        int? bus = null;
        bool? seekPenalty = null;
        var output = new byte[1024];
        if (Ask(handle, StorageDeviceProperty, output, out var returned) && returned >= 32)
        {
            bus = BitConverter.ToInt32(output, 28);
        }

        Array.Clear(output);
        if (Ask(handle, StorageDeviceSeekPenaltyProperty, output, out returned) && returned >= 9)
        {
            seekPenalty = output[8] != 0;
        }

        return (bus, seekPenalty);
    }

    private static bool Ask(SafeFileHandle handle, int propertyId, byte[] output, out int returned)
    {
        // STORAGE_PROPERTY_QUERY: PropertyId, QueryType (0 = PropertyStandardQuery), one parameter byte.
        var query = new byte[12];
        BitConverter.TryWriteBytes(query.AsSpan(0, 4), propertyId);
        return DeviceIoControl(handle, IoctlStorageQueryProperty, query, query.Length, output, output.Length, out returned, IntPtr.Zero);
    }

    private static long TotalMemoryBytes()
    {
        try
        {
            // MEMORYSTATUSEX: dwLength at 0, ullTotalPhys at 8; the structure is 64 bytes.
            var status = new byte[64];
            BitConverter.TryWriteBytes(status.AsSpan(0, 4), status.Length);
            if (GlobalMemoryStatusEx(status))
            {
                return (long)BitConverter.ToUInt64(status, 8);
            }
        }
        catch (Exception)
        {
            // Fall through to the runtime's view of memory.
        }

        try
        {
            return Math.Max(0, GC.GetGCMemoryInfo().TotalAvailableMemoryBytes);
        }
        catch (Exception)
        {
            return 0;
        }
    }

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern SafeFileHandle CreateFileW(string fileName, uint desiredAccess, uint shareMode,
        IntPtr securityAttributes, uint creationDisposition, uint flagsAndAttributes, IntPtr templateFile);

    [DllImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool DeviceIoControl(SafeFileHandle device, uint ioControlCode, byte[] inBuffer, int inBufferSize,
        [Out] byte[] outBuffer, int outBufferSize, out int bytesReturned, IntPtr overlapped);

    [DllImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool GlobalMemoryStatusEx([In, Out] byte[] buffer);
}
