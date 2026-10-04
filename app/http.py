"""One shared HTTP client for every outbound call (YouTube, Google OAuth, Analytics)."""
import httpx

# Force IPv4: on networks where IPv6 is advertised but unreachable, httpx waits out a timeout per
# IPv6 address (~2 min total) before falling back. The shared client also reuses TLS connections.
client = httpx.Client(transport=httpx.HTTPTransport(local_address="0.0.0.0", retries=2),
                      timeout=httpx.Timeout(20, connect=8))
