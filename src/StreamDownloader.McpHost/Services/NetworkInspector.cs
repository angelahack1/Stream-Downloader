using System.Net;
using System.Net.Sockets;

namespace StreamDownloader.Services;

internal sealed class NetworkInspector
{
    private readonly bool _blockPrivate;

    public NetworkInspector(bool blockPrivate)
    {
        _blockPrivate = blockPrivate;
    }

    public async Task ValidatePublicUrlAsync(string rawUrl, CancellationToken cancellationToken)
    {
        var uri = ValidateUrl(rawUrl);
        await ValidateHostAsync(uri, cancellationToken).ConfigureAwait(false);
    }

    private Uri ValidateUrl(string rawUrl)
    {
        if (rawUrl.Length > 8192 || !Uri.TryCreate(rawUrl, UriKind.Absolute, out var uri))
        {
            throw new InvalidOperationException("URL is invalid or too long.");
        }

        if (uri.Scheme is not ("http" or "https") || !string.IsNullOrEmpty(uri.UserInfo))
        {
            throw new InvalidOperationException("Only credential-free HTTP/HTTPS URLs are accepted.");
        }

        return uri;
    }

    private async Task ValidateHostAsync(Uri uri, CancellationToken cancellationToken)
    {
        if (!_blockPrivate)
        {
            return;
        }

        var addresses = await Dns.GetHostAddressesAsync(uri.DnsSafeHost, cancellationToken).ConfigureAwait(false);
        if (addresses.Length == 0 || addresses.Any(IsPrivateOrSpecial))
        {
            throw new InvalidOperationException("URL resolves to a private, loopback, link-local, or special-use address.");
        }
    }

    internal static bool IsPrivateOrSpecial(IPAddress address)
    {
        if (address.IsIPv4MappedToIPv6) address = address.MapToIPv4();
        if (IPAddress.IsLoopback(address) || address.Equals(IPAddress.Any) || address.Equals(IPAddress.IPv6Any))
        {
            return true;
        }

        if (address.AddressFamily == AddressFamily.InterNetworkV6)
        {
            if (address.IsIPv6LinkLocal || address.IsIPv6Multicast || address.IsIPv6SiteLocal)
            {
                return true;
            }

            var bytes6 = address.GetAddressBytes();
            return (bytes6[0] & 0xFE) == 0xFC;
        }

        var bytes = address.GetAddressBytes();
        return bytes[0] switch
        {
            0 or 10 or 127 => true,
            100 when bytes[1] is >= 64 and <= 127 => true,
            169 when bytes[1] == 254 => true,
            172 when bytes[1] is >= 16 and <= 31 => true,
            192 when bytes[1] == 168 => true,
            >= 224 => true,
            _ => false
        };
    }
}
