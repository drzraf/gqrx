# Gqrx Shared-Memory IQ Transport Protocol

This document describes the inter-process IQ data streaming protocol used when
gqrx is built with `WITH_SHM_IQ_SINK=ON` (the default on Linux).  It is
precise enough to implement or verify a compatible consumer (e.g. the
`fix/memfd-spsc` branch of `mhassell/rtl_433`) without reading the gqrx source.

---

## Transport overview

| Property | Value |
|---|---|
| Kernel primitive | `memfd_create("gqrx_iq_ringbuf", MFD_CLOEXEC)` |
| Notification    | `eventfd(0, EFD_CLOEXEC \| EFD_SEMAPHORE \| EFD_NONBLOCK)` |
| Mapping         | `mmap(MAP_SHARED)` on the memfd |
| Ring type       | Single-producer / single-consumer (SPSC), lock-free |
| Synchronisation | GCC built-in atomic `__ATOMIC_ACQUIRE` / `__ATOMIC_RELEASE` on head/tail |

Because `memfd_create` produces an **anonymous** file descriptor (no filesystem
path), the producer must explicitly hand the two fds to the consumer via one of
the mechanisms described below.

---

## Shared-memory layout

The memfd is `ftruncate`'d to:

```
total_size = sizeof(shm_ringbuf_header_t) + bufsize
```

where `bufsize` is currently **32 MiB** (33 554 432 bytes).

### Header (`shm_ringbuf_header_t`)

Placed at offset 0 of the mapping.  All fields are little-endian on x86/ARM.

| Offset | Type     | Name          | Value / meaning |
|--------|----------|---------------|-----------------|
| 0      | uint32_t | `magic`       | `0x52494E47` ("RING") |
| 4      | uint32_t | `version`     | `1` |
| 8      | uint64_t | `head`        | Producer write index (bytes, monotonically increasing) |
| 16     | uint64_t | `tail`        | Consumer read  index (bytes, monotonically increasing) |
| 24     | uint64_t | `bufsize`     | Ring data size in bytes (32 MiB = 33 554 432) |
| 32     | uint32_t | `sample_rate` | Sample rate in Hz (e.g. 1 800 000) |
| 36     | uint32_t | `sample_size` | Bytes per IQ pair: **2** for CU8, 4 for CS16 |
| 40     | —        | —             | End of header; ring data follows immediately |

`sizeof(shm_ringbuf_header_t)` = **40 bytes** (no padding on any supported
architecture since all fields are naturally aligned).

### Ring data region

Starts at `mapping_base + sizeof(shm_ringbuf_header_t)` = `mapping_base + 40`.
Size = `hdr->bufsize` bytes.

The **logical write position** of the producer is:

```c
write_pos = head % bufsize;   /* head is the raw monotonic byte counter */
```

The **logical read position** of the consumer is:

```c
read_pos  = tail % bufsize;
```

Bytes available to read: `head - tail`.
Free space to write:     `bufsize - (head - tail)`.

Wraps around transparently; both the producer and consumer split their
memcpy at the end of the ring if needed.

---

## IQ sample format: CU8

gqrx writes **CU8** (complex unsigned 8-bit) interleaved IQ.

Each IQ pair is **2 bytes**: first byte = I, second byte = Q.

Encoding:

```
byte_value = clamp(float_value, -1.0, +1.0) * 127.5 + 127.5
```

Inverse (decoding):

```
float_value = (byte_value - 127.5) / 127.5
```

Range mapping:

| Float | Byte |
|-------|------|
| +1.0  | 255  |
|  0.0  | 127 or 128 (rounds) |
| -1.0  |   0  |

This is the **standard symmetric CU8** convention used by RTL-SDR hardware and
expected by rtl_433.  The centre value is 127.5, not 128.

---

## Eventfd notification semantics

The producer calls:

```c
uint64_t one = 1;
write(event_fd, &one, sizeof(one));
```

after every successful `shm_ringbuf_write()` call.

The eventfd was created with `EFD_SEMAPHORE | EFD_NONBLOCK`.  In semaphore
mode, each `read()` of 8 bytes decrements the counter by exactly 1.  The
consumer should:

1. Call `poll(event_fd, POLLIN, timeout_ms)` to wait for data.
2. Call `read(event_fd, &val, 8)` to drain **one** token.
3. Read as many bytes as available from the ring buffer.

EAGAIN from the `write()` side (counter saturated at UINT64_MAX − 1) is silently
ignored by the producer — consumers that fall behind will see the counter
clamped but will still drain data correctly from the ring once they wake up.

---

## Fd-handoff mechanisms

Because `memfd_create` fds are anonymous, the consumer must receive the
`memfd` and `event_fd` descriptors via one of the following methods:

### Method A — Inherited fd (child process)

The parent (gqrx) calls `shm_ringbuf_prepare_child_fds()` before `fork()`+
`exec()`, which:

1. `dup()`s both fds to new descriptors **without** `FD_CLOEXEC`.
2. Sets the environment variables:

```
GQRX_IQ_MEMFD=<fd_number>
GQRX_IQ_EVENTFD=<fd_number>
```

The child process reads these variables and calls:

```c
int memfd   = atoi(getenv("GQRX_IQ_MEMFD"));
int eventfd = atoi(getenv("GQRX_IQ_EVENTFD"));
shm_ringbuf_attach(memfd, eventfd, &ctx);
```

### Method B — SCM_RIGHTS (unrelated already-running process)

The producer calls `shm_ringbuf_send_fds(ctx, NULL)` (or
`shm_ringbuf_send_fds(ctx, "/path/to/socket")`).

This function:

1. Creates a `SOCK_STREAM` Unix domain socket at the path given by the
   `GQRX_IQ_SOCK` environment variable, or `/tmp/gqrx_iq.sock` by default.
2. Listens for exactly one incoming connection.
3. Sends both fds (`memfd`, `event_fd`) as `SCM_RIGHTS` ancillary data over
   that connection (with a 1-byte dummy payload required by POSIX).
4. Closes and `unlink`s the socket.

The consumer connects to the socket, receives the message via `recvmsg()`:

```c
int fds[2];  /* fds[0] = memfd, fds[1] = eventfd */
/* ... standard SCM_RIGHTS recvmsg boilerplate ... */
shm_ringbuf_attach(fds[0], fds[1], &ctx);
```

The socket path can be overridden by setting `GQRX_IQ_SOCK` in the gqrx
environment before it starts streaming.

---

## Consumer attach sequence (summary)

```c
/* 1. Obtain memfd and event_fd by env var (Method A) or SCM_RIGHTS (Method B) */
int memfd   = ...;
int eventfd = ...;

/* 2. Attach */
shm_ringbuf_t ctx;
memset(&ctx, 0, sizeof(ctx));
if (shm_ringbuf_attach(memfd, eventfd, &ctx) < 0) { /* error */ }

/* 3. Verify header */
assert(ctx.hdr->magic   == SHM_RINGBUF_MAGIC);    /* 0x52494E47 */
assert(ctx.hdr->version == SHM_RINGBUF_VERSION);  /* 1           */
assert(ctx.hdr->sample_size == 2);                /* CU8         */

/* 4. Read loop */
uint8_t buf[65536];
while (running) {
    ssize_t n = shm_ringbuf_read(&ctx, buf, sizeof(buf), -1 /* block */);
    if (n > 0)
        process_cu8_iq(buf, n / 2 /* number of IQ pairs */);
}

/* 5. Cleanup */
shm_ringbuf_close(&ctx);
```

---

## Known limitations and follow-up work

- The `send_fds()` path is **synchronous** (blocking accept).  A non-blocking
  background-thread version is needed if gqrx should not stall waiting for the
  consumer to connect.
- Only **one** consumer is supported at a time (SPSC ring).  Multiple consumers
  would require separate ring buffers or a fanout mechanism.
- The eventfd notification is best-effort: the counter saturates at
  UINT64_MAX − 1.  A very fast producer writing tiny blocks will eventually hit
  this limit; the recommended mitigation is to batch writes (already done in the
  `shm_iq_sink`).
- `WITH_SHM_IQ_SINK` is forced to `OFF` on non-Linux platforms since
  `memfd_create` and `eventfd` are Linux-specific (defaults to `ON` on Linux).
