using System.Security.Cryptography;
using System.Text;

namespace StreamDownloader.Services;

internal static class SecretVault
{
    private static readonly byte[] Entropy = Encoding.UTF8.GetBytes("Stream-Downloader/v1");

    public static string Protect(string value)
    {
        ArgumentNullException.ThrowIfNull(value);
        var bytes = Encoding.UTF8.GetBytes(value);
        var encrypted = ProtectedData.Protect(bytes, Entropy, DataProtectionScope.CurrentUser);
        CryptographicOperations.ZeroMemory(bytes);
        return Convert.ToBase64String(encrypted);
    }

    public static string Unprotect(string protectedValue)
    {
        ArgumentNullException.ThrowIfNull(protectedValue);
        var encrypted = Convert.FromBase64String(protectedValue);
        var bytes = ProtectedData.Unprotect(encrypted, Entropy, DataProtectionScope.CurrentUser);
        try
        {
            return Encoding.UTF8.GetString(bytes);
        }
        finally
        {
            CryptographicOperations.ZeroMemory(bytes);
        }
    }
}
