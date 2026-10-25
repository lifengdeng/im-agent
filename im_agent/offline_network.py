"""Process audit guard used only by the offline candidate service profile."""
import ipaddress
import socket
import sys


def install(role, *, business_proxy=False, external_https=()):
    if role not in {"worker", "transport"}:
        raise ValueError("offline_role_invalid")
    local_names = {"localhost", "0.0.0.0", socket.gethostname()}
    peer = ("worker", 8080) if role == "transport" else ("transport", 8090) if business_proxy else None
    # Explicit preparation profile only. IM transport never receives an
    # external allowance; an empty set retains the original offline policy.
    external = frozenset(external_https)
    if (role != "worker" and external or any(not isinstance(h, str) or not h or
            any(c not in "abcdefghijklmnopqrstuvwxyz0123456789.-" for c in h) for h in external)):
        raise ValueError("external_https_policy_invalid")

    def loopback(host):
        try: return ipaddress.ip_address(host).is_loopback
        except (ValueError, TypeError): return False

    def audit(name, args):
        if name in {"socket.getaddrinfo", "socket.gethostbyname", "socket.gethostbyaddr"}:
            host = args[0]
            if not (host in local_names or loopback(host) or host in external or host == ("worker" if role == "transport" else "transport" if business_proxy else None)):
                raise PermissionError("offline_external_resolution_disabled")
        if name in {"socket.connect", "socket.sendto"}:
            address = args[-1]
            if isinstance(address, tuple):
                host, port = address[:2]
                # Resolve only the fixed Docker alias at connection time. A
                # replacement container can legitimately have a different IP;
                # retaining an old IP would strand the surviving peer forever.
                allowed = loopback(host)
                if not allowed and peer and port == peer[1]:
                    allowed = host in {entry[4][0] for entry in socket.getaddrinfo(*peer, type=socket.SOCK_STREAM)}
                if not allowed and external and port == 443:
                    allowed = host in {entry[4][0] for name in external for entry in socket.getaddrinfo(name, 443, type=socket.SOCK_STREAM)}
                if not allowed:
                    raise PermissionError("offline_external_connection_disabled")
    sys.addaudithook(audit)
