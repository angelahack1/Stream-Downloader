using System.Text.RegularExpressions;

namespace StreamDownloader.Services;

internal sealed partial class PathPolicy
{
    private static readonly HashSet<string> ReservedNames = new(StringComparer.OrdinalIgnoreCase)
    {
        "CON", "PRN", "AUX", "NUL",
        "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
        "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9"
    };

    private readonly IReadOnlyList<string> _allowedRoots;
    private readonly bool _restrictToAllowedRoots;

    public PathPolicy(IReadOnlyList<string> allowedRoots, bool restrictToAllowedRoots = false)
    {
        _allowedRoots = allowedRoots.Select(NormalizeDirectory).ToArray();
        _restrictToAllowedRoots = restrictToAllowedRoots;
    }

    public IReadOnlyList<string> AllowedRoots => _allowedRoots;

    public string ValidateDirectory(string? requested, string defaultDirectory)
    {
        var path = string.IsNullOrWhiteSpace(requested) ? defaultDirectory : requested.Trim();
        if (!Path.IsPathFullyQualified(path) || path.StartsWith("\\\\", StringComparison.Ordinal))
        {
            throw new InvalidOperationException("Destination must be an absolute local path; UNC paths are disabled.");
        }

        var full = NormalizeDirectory(path);
        if (_restrictToAllowedRoots && !_allowedRoots.Any(root => IsWithin(full, root)))
        {
            throw new InvalidOperationException(
                $"Destination is outside configured roots: {string.Join(", ", _allowedRoots)}");
        }

        RejectExistingReparsePoints(full);
        return Path.GetPathRoot(full) == full ? full : full.TrimEnd(Path.DirectorySeparatorChar);
    }

    public string ValidateFileName(string value)
    {
        if (string.IsNullOrWhiteSpace(value))
        {
            throw new InvalidOperationException("A non-empty filename is required.");
        }

        var name = value;
        if (Path.GetFileName(name) != name || name.Contains(':', StringComparison.Ordinal))
        {
            throw new InvalidOperationException("Filename must not contain a path or alternate data stream.");
        }

        if (name.Length > 180 || name.EndsWith(' ') || name.EndsWith('.'))
        {
            throw new InvalidOperationException("Filename is too long or has an unsafe Windows suffix.");
        }

        if (name.IndexOfAny(Path.GetInvalidFileNameChars()) >= 0 || ControlCharacters().IsMatch(name))
        {
            throw new InvalidOperationException("Filename contains invalid characters.");
        }

        var stem = name.Split('.')[0].TrimEnd(' ');
        if (ReservedNames.Contains(stem))
        {
            throw new InvalidOperationException("Filename uses a reserved Windows device name.");
        }

        return name;
    }

    private static bool IsWithin(string candidate, string root) =>
        candidate.Equals(root, StringComparison.OrdinalIgnoreCase) ||
        candidate.StartsWith(root, StringComparison.OrdinalIgnoreCase);

    private static string NormalizeDirectory(string path)
    {
        var full = Path.GetFullPath(path).TrimEnd(Path.DirectorySeparatorChar) + Path.DirectorySeparatorChar;
        return full;
    }

    private static void RejectExistingReparsePoints(string path)
    {
        var current = Path.GetPathRoot(path) ?? throw new InvalidOperationException("Destination has no drive root.");
        foreach (var segment in path[(current.Length)..].Split(Path.DirectorySeparatorChar, StringSplitOptions.RemoveEmptyEntries))
        {
            current = Path.Combine(current, segment);
            if (!Directory.Exists(current) && !File.Exists(current))
            {
                break;
            }

            var attributes = File.GetAttributes(current);
            if ((attributes & FileAttributes.ReparsePoint) != 0)
            {
                throw new InvalidOperationException($"Reparse points are not allowed in destinations: {current}");
            }
            if ((attributes & FileAttributes.Directory) == 0)
            {
                throw new InvalidOperationException("Destination contains a file where a directory is required.");
            }
        }
    }

    [GeneratedRegex("[\\x00-\\x1F]")]
    private static partial Regex ControlCharacters();
}
