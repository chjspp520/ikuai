"""虚拟机屏幕快照：直接连虚拟机 VNC 抓一帧，编成 PNG。

为什么不用 vncdotool / Pillow
------------------------------
`vncdotool` 会带进 twisted，`Pillow` 又是另一个大包；而我们需要的东西非常少：

    连接 → RFB 握手 → VNC 认证(DES) → 声明"只用 Raw 编码" → 要一帧 → 编 PNG

所以这里用标准库（socket / struct / zlib）自己实现，DES 走 Home Assistant
自带依赖 ``cryptography``（单个 DES = 三个相同密钥的 3DES，因为新版
cryptography 已把单 DES 移除）。集成体积零增长、不需要额外 pip 包。

只读特性
--------
本模块只做"请求一帧画面"这一件事：不注册键鼠、不进控制台会话、不占用
「远程控制」开关的通道，所以**总开关关闭时静态画面仍然可用**（只读信息）。
每次抓帧是一条独立的 VNC 连接，QEMU 侧 ``shared`` 默认开启，多客户端互不影响。
"""
from __future__ import annotations

import logging
import socket
import struct
import zlib

_LOGGER = logging.getLogger(__name__)

_RFB_VERSION = b"RFB 003.008\n"
# 声明的像素格式：32bpp、depth 24、小端、真彩、RGB 顺序（关键：red_shift=0 → 每像素第 1 字节就是 R）
# 布局：bpp(1) depth(1) big-endian(1) true-colour(1) r-max(2) g-max(2) b-max(2) r-shift(1) g-shift(1) b-shift(1) pad(3)
_PIXEL_FORMAT = struct.pack(">BBBBHHHBBB3x", 32, 24, 0, 1, 255, 255, 255, 0, 8, 16)
_ENCODING_RAW = 0


class VncSnapshotError(RuntimeError):
    """抓帧失败（连接/认证/协议/编码等问题），消息为可直接展示的中文原因。"""


# --------------------------------------------------------------------------- #
# VNC 认证用的 DES
# --------------------------------------------------------------------------- #
def _des_encrypt_ecb(key8: bytes, data: bytes) -> bytes:
    """单次 DES-ECB 加密（VNC 认证专用）。

    新版 cryptography 移除了单 DES，这里用"三段相同密钥"的 3DES 等价实现。
    """
    try:
        from cryptography.hazmat.decrepit.ciphers.algorithms import TripleDES
    except ImportError:  # pragma: no cover - 旧版 cryptography
        from cryptography.hazmat.primitives.ciphers.algorithms import TripleDES
    from cryptography.hazmat.primitives.ciphers import Cipher, modes

    encryptor = Cipher(TripleDES(key8 * 3), modes.ECB()).encryptor()
    return encryptor.update(data) + encryptor.finalize()


def vnc_auth_key(password: str | bytes) -> bytes:
    """VNC 口令 → DES 密钥：最多 8 字节、补零，且**每个字节按位反转**。"""
    raw = password.encode("latin-1", "replace") if isinstance(password, str) else password
    raw = raw[:8].ljust(8, b"\x00")
    out = bytearray()
    for byte in raw:
        flipped = 0
        for bit in range(8):
            flipped = (flipped << 1) | ((byte >> bit) & 1)
        out.append(flipped)
    return bytes(out)


def vnc_auth_response(password: str | bytes, challenge: bytes) -> bytes:
    """VNC 认证应答：用口令密钥 DES-ECB 加密 16 字节挑战值。"""
    return _des_encrypt_ecb(vnc_auth_key(password), bytes(challenge[:16]))


# --------------------------------------------------------------------------- #
# PNG 编码（8bit RGB，无依赖）
# --------------------------------------------------------------------------- #
def _png_chunk(tag: bytes, data: bytes) -> bytes:
    return (struct.pack(">I", len(data)) + tag + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))


def _encode_png(width: int, height: int, framebuffer: bytearray) -> bytes:
    """把 32bpp（RGBX）帧缓冲编成 PNG（去掉每像素第 4 字节）。"""
    stride = width * 4
    rows = []
    for y in range(height):
        offset = y * stride
        row = bytes(framebuffer[offset:offset + stride])
        rows.append(b"\x00" + b"".join(
            [row[i:i + 3] for i in range(0, len(row), 4)]))
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n"
            + _png_chunk(b"IHDR", ihdr)
            + _png_chunk(b"IDAT", zlib.compress(b"".join(rows), 6))
            + _png_chunk(b"IEND", b""))


# --------------------------------------------------------------------------- #
# RFB 取帧
# --------------------------------------------------------------------------- #
def _recv_exact(sock: socket.socket, size: int) -> bytes:
    chunks = []
    remaining = size
    while remaining > 0:
        chunk = sock.recv(min(remaining, 262144))
        if not chunk:
            raise VncSnapshotError("虚拟机 VNC 连接被对端关闭")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks) if len(chunks) > 1 else chunks[0]


def _read_security_reason(sock: socket.socket) -> str:
    length = struct.unpack(">I", _recv_exact(sock, 4))[0]
    if not length or length > 4096:
        return ""
    return _recv_exact(sock, length).decode("utf-8", "replace")


def _authenticate(sock: socket.socket, password: str, challenge: bytes) -> None:
    sock.sendall(vnc_auth_response(password, challenge))
    result = struct.unpack(">I", _recv_exact(sock, 4))[0]
    if result != 0:
        raise VncSnapshotError(
            "VNC 认证失败：虚拟机 VNC 密码不正确（可在 iKuai 的虚拟机设置里核对）")


def _handshake(sock: socket.socket, password: str) -> str:
    """完成版本协商与认证，返回服务端版本字符串。"""
    server_version = _recv_exact(sock, 12)
    if not server_version.startswith(b"RFB "):
        raise VncSnapshotError("目标端口不是 VNC 服务（响应不是 RFB 协议）")
    sock.sendall(_RFB_VERSION)
    try:
        minor = int(server_version[8:11])
    except ValueError:
        minor = 8

    if minor >= 7:
        count = _recv_exact(sock, 1)[0]
        if count == 0:
            raise VncSnapshotError(f"虚拟机 VNC 拒绝连接：{_read_security_reason(sock)}")
        types = list(_recv_exact(sock, count))
        if 2 in types:
            if not password:
                raise VncSnapshotError(
                    "该虚拟机设置了 VNC 密码，但集成没取到密码（请检查 iKuai 虚拟机设置）")
            sock.sendall(bytes([2]))
            _authenticate(sock, password, _recv_exact(sock, 16))
            return server_version.decode("ascii", "replace").strip()
        if 1 in types:
            sock.sendall(bytes([1]))
            # 认证结果（None 认证也要读，部分实现会下发）
            try:
                _recv_exact(sock, 4)
            except VncSnapshotError:
                pass
            return server_version.decode("ascii", "replace").strip()
        raise VncSnapshotError(
            f"该虚拟机 VNC 使用了本集成不支持的安全类型：{types}")

    # RFB 3.3：服务端直接下发一个 4 字节安全类型
    sec_type = struct.unpack(">I", _recv_exact(sock, 4))[0]
    if sec_type == 2:
        if not password:
            raise VncSnapshotError("该虚拟机设置了 VNC 密码，但集成没取到密码")
        _authenticate(sock, password, _recv_exact(sock, 16))
    elif sec_type != 1:
        raise VncSnapshotError(f"该虚拟机 VNC 使用了本集成不支持的安全类型：{sec_type}")
    return server_version.decode("ascii", "replace").strip()


def _read_server_init(sock: socket.socket) -> tuple[int, int]:
    head = _recv_exact(sock, 24)
    width, height = struct.unpack(">HH", head[:4])
    name_len = struct.unpack(">I", head[20:24])[0]
    if name_len:
        _recv_exact(sock, name_len)
    if width <= 0 or height <= 0 or width > 8192 or height > 8192:
        raise VncSnapshotError(f"虚拟机分辨率异常：{width}x{height}")
    return width, height


def _request_frame(sock: socket.socket, width: int, height: int,
                   version: str) -> bytearray:
    """声明只用 Raw 编码，要一帧整屏画面并读回。"""
    sock.sendall(b"\x00\x00\x00\x00" + _PIXEL_FORMAT)
    if not version.startswith("RFB 003.003"):  # 3.3 没有 SetEncodings（也不支持扩展编码）
        sock.sendall(struct.pack(">BBH", 2, 0, 1) + struct.pack(">i", _ENCODING_RAW))
    sock.sendall(struct.pack(">BBHHHH", 3, 0, 0, 0, width, height))

    frame = bytearray(width * height * 4)
    stride = width * 4
    while True:
        msg_type = _recv_exact(sock, 1)[0]
        if msg_type == 0:                       # FramebufferUpdate
            _recv_exact(sock, 1)
            rects = struct.unpack(">H", _recv_exact(sock, 2))[0]
            full = False
            for _ in range(rects):
                x, y, w, h, encoding = struct.unpack(">HHHHi", _recv_exact(sock, 12))
                if encoding == _ENCODING_RAW:
                    data = _recv_exact(sock, w * h * 4)
                    for line in range(h):
                        src = line * w * 4
                        dst = (y + line) * stride + x * 4
                        frame[dst:dst + w * 4] = data[src:src + w * 4]
                    if x == 0 and y == 0 and w == width and h == height:
                        full = True
                elif encoding == -223:          # DesktopSize（未声明，防御性忽略）
                    width, height = w, h
                    frame = bytearray(width * height * 4)
                    stride = width * 4
                else:
                    raise VncSnapshotError(f"该虚拟机 VNC 返回了不支持的编码：{encoding}")
            if full:
                return frame
        elif msg_type == 3:                     # ServerCutText
            _recv_exact(sock, 3)
            length = struct.unpack(">I", _recv_exact(sock, 4))[0]
            if length:
                _recv_exact(sock, length)
        elif msg_type == 2:                     # Bell
            continue
        elif msg_type == 1:                     # SetColourMapEntries（真彩下通常不下发）
            _recv_exact(sock, 3)
            entries = struct.unpack(">H", _recv_exact(sock, 2))[0]
            if entries:
                _recv_exact(sock, entries * 6)
        else:
            raise VncSnapshotError(f"该虚拟机 VNC 返回了未知消息类型：{msg_type}")


def grab_png(host: str, port: int, password: str = "", timeout: float = 8.0,
             shared: bool = True) -> tuple[int, int, bytes]:
    """连一次虚拟机的 VNC，抓一帧并返回 ``(宽, 高, PNG 字节)``。

    阻塞实现（在 executor 里调用）。失败一律抛 :class:`VncSnapshotError`，
    调用方拿消息直接给用户看即可。
    """
    try:
        with socket.create_connection((host, int(port)), timeout=timeout) as sock:
            sock.settimeout(timeout)
            try:
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except OSError:  # pragma: no cover - 极少数平台
                pass
            version = _handshake(sock, password)
            sock.sendall(bytes([1 if shared else 0]))       # ClientInit
            width, height = _read_server_init(sock)
            frame = _request_frame(sock, width, height, version)
    except VncSnapshotError:
        raise
    except (OSError, struct.error, zlib.error) as err:
        raise VncSnapshotError(f"连接虚拟机 VNC 失败：{err}") from err
    return width, height, _encode_png(width, height, frame)
