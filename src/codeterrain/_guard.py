"""Best-effort network guard for code under analysis: loopback-only sockets.

Installed inside the sandboxed subprocess by ``sandbox.BOOTSTRAP`` (and by the ``sitecustomize`` the
sandbox writes, so child interpreters inherit it). It is a guard against accidental network calls
from the target's code, not a security boundary against hostile code: references to socket
functions taken before ``install()`` ran, C extensions that open sockets themselves, uvloop and
aiodns are not covered. Stdlib only; importing this module changes nothing until ``install()``.

``install()`` also replaces ``_socket.socket`` (and the ``SocketType`` aliases) with a guarded
subclass, so ``isinstance(sock, _socket.socket)`` is False for sockets made by ``socket.socket``.
"""

from __future__ import annotations

import _socket
import ipaddress
import socket
import sys
import warnings

_LOOPBACK_NAMES = {"localhost", "localhost.localdomain", "ip6-localhost", ""}
_INET_FAMILIES = {socket.AF_INET, socket.AF_INET6}

#: socket.socket methods that can originate traffic to an address; each is replaced.
GUARDED_METHODS = ("connect", "connect_ex", "sendto", "sendmsg")
#: module-level functions in socket (and _socket) that resolve or reach a name; each is replaced.
LOOKUP_FUNCTIONS = (
    "getaddrinfo",
    "gethostbyname",
    "gethostbyname_ex",
    "gethostbyaddr",
    "getnameinfo",
)
GUARDED_FUNCTIONS = (*LOOKUP_FUNCTIONS, "create_connection")


class SandboxNetworkBlocked(OSError):
    pass


def _is_loopback(host):
    if host is None:
        return True
    if isinstance(host, (bytes, bytearray)):
        host = bytes(host).decode("ascii", "replace")
    host = str(host)
    if host.lower() in _LOOPBACK_NAMES:
        return True
    try:
        addr = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        return False
    mapped = getattr(addr, "ipv4_mapped", None)
    return (mapped or addr).is_loopback


def _refuse(host, port):
    raise SandboxNetworkBlocked(
        f"codeterrain sandbox: non-loopback network access is blocked ({host}:{port})"
    )


def _check_host(host, port=""):
    if not _is_loopback(host):
        _refuse(host, port)


def _check_address(family, address):
    if family in _INET_FAMILIES and isinstance(address, tuple) and address:
        _check_host(address[0], address[1] if len(address) > 1 else "")
        return
    _refuse(address, "")


def _name_of(sockaddr):
    return sockaddr[0] if isinstance(sockaddr, tuple) and sockaddr else sockaddr


def _build_methods():
    connect_o, connect_ex_o = socket.socket.connect, socket.socket.connect_ex
    sendto_o, sendmsg_o = socket.socket.sendto, getattr(socket.socket, "sendmsg", None)

    def connect(self, address):
        _check_address(self.family, address)
        return connect_o(self, address)

    def connect_ex(self, address):
        _check_address(self.family, address)
        return connect_ex_o(self, address)

    def sendto(self, data, *args):
        _check_address(self.family, args[-1])
        return sendto_o(self, data, *args)

    def sendmsg(self, buffers, *args, **kwargs):
        address = args[2] if len(args) > 2 else kwargs.get("address")
        if address is not None:
            _check_address(self.family, address)
        return sendmsg_o(self, buffers, *args, **kwargs)

    built = {"connect": connect, "connect_ex": connect_ex, "sendto": sendto}
    if sendmsg_o is not None:
        built["sendmsg"] = sendmsg
    return built


def _build_lookups(module):
    """Guarded wrappers for the name-resolution functions of ``module`` (socket or _socket)."""
    found = {name: getattr(module, name) for name in LOOKUP_FUNCTIONS if hasattr(module, name)}

    def getaddrinfo(host, *args, **kwargs):
        _check_host(host, args[0] if args else kwargs.get("port", ""))
        return found["getaddrinfo"](host, *args, **kwargs)

    def gethostbyname(host):
        _check_host(host)
        return found["gethostbyname"](host)

    def gethostbyname_ex(host):
        _check_host(host)
        return found["gethostbyname_ex"](host)

    def gethostbyaddr(host):
        _check_host(host)
        return found["gethostbyaddr"](host)

    def getnameinfo(sockaddr, flags):
        _check_host(_name_of(sockaddr))
        return found["getnameinfo"](sockaddr, flags)

    wrappers = {
        "getaddrinfo": getaddrinfo,
        "gethostbyname": gethostbyname,
        "gethostbyname_ex": gethostbyname_ex,
        "gethostbyaddr": gethostbyaddr,
        "getnameinfo": getnameinfo,
    }
    return {name: wrappers[name] for name in found}


def _build_create_connection():
    original = socket.create_connection

    def create_connection(address, *args, **kwargs):
        _check_host(_name_of(address), address[1] if len(address) > 1 else "")
        return original(address, *args, **kwargs)

    return create_connection


_methods = _build_methods()
_lookups = _build_lookups(socket)
_raw_lookups = _build_lookups(_socket)
create_connection = _build_create_connection()

connect = _methods["connect"]
connect_ex = _methods["connect_ex"]
sendto = _methods["sendto"]
sendmsg = _methods.get("sendmsg")
getaddrinfo = _lookups["getaddrinfo"]


def _guarded_raw_socket():
    base = _socket.socket
    return type("socket", (base,), dict(_methods))


def _wrap_proactor():
    """Windows' proactor loop connects and sends through overlapped I/O, past ``socket.connect``."""
    if sys.platform != "win32":
        return
    try:
        from asyncio import windows_events
    except ImportError:
        return
    proactor = windows_events.IocpProactor
    if getattr(proactor.connect, "_codeterrain_guard", False):
        return
    connect_o, sendto_o = proactor.connect, proactor.sendto
    sendto_addr_o = getattr(proactor, "sendto_addr", None)

    def connect(self, conn, address):
        _check_address(conn.family, address)
        return connect_o(self, conn, address)

    def sendto(self, conn, buf, flags=0, addr=None):
        if addr is not None:
            _check_address(conn.family, addr)
        return sendto_o(self, conn, buf, flags, addr)

    def sendto_addr(self, conn, buf, addr, *args, **kwargs):
        _check_address(conn.family, addr)
        return sendto_addr_o(self, conn, buf, addr, *args, **kwargs)

    wrapped = {"connect": connect, "sendto": sendto}
    if sendto_addr_o is not None:
        wrapped["sendto_addr"] = sendto_addr
    for name, fn in wrapped.items():
        fn._codeterrain_guard = True
        setattr(proactor, name, fn)


def proactor_guarded():
    """True when the asyncio proactor's connect and sendto are wrapped (always True off Windows)."""
    if sys.platform != "win32":
        return True
    try:
        from asyncio import windows_events
    except ImportError:
        return True
    proactor = windows_events.IocpProactor
    return all(
        getattr(getattr(proactor, name), "_codeterrain_guard", False)
        for name in ("connect", "sendto")
    )


def install():
    """Replace the socket entry points. Idempotent; call once, before the target is imported."""
    if getattr(socket.socket, "_codeterrain_guard", False):
        return
    for name, fn in _methods.items():
        setattr(socket.socket, name, fn)
    socket.socket._codeterrain_guard = True
    for name, fn in _lookups.items():
        setattr(socket, name, fn)
    for name, fn in _raw_lookups.items():
        setattr(_socket, name, fn)
    socket.create_connection = create_connection
    guarded = _guarded_raw_socket()
    _socket.socket = _socket.SocketType = socket.SocketType = guarded
    _wrap_proactor()


def use_selector_event_loop():
    """Prefer the selector loop on Windows; the proactor is guarded too, this is a nicety."""
    import asyncio

    policy = getattr(asyncio, "WindowsSelectorEventLoopPolicy", None)
    if policy is not None:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            asyncio.set_event_loop_policy(policy())
