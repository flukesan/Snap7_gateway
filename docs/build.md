# Building and installing the Snap7 library

The gateway talks S7 through `python-snap7`. What that package needs from the
host differs by platform and by version, so read section 1 before doing anything
else.

## 1. Which parts are native

`python-snap7` 3.x is **not** a pure wrapper any more:

* **Client role** (`snap7.client.Client`) — uses the native Snap7 C library
  (`snap7.dll` / `libsnap7.so`). This is what talks to your real PLCs.
* **Server role** (`snap7.server.Server`) — implemented in pure Python in 3.x.
  The virtual S7 CPU therefore needs no native library at all.

Consequence: a host that cannot load the C library can still *host* the virtual
CPU but cannot *poll* a PLC. If the gateway logs
`could not load the Snap7 library`, this section is where to look.

Recent `python-snap7` wheels bundle a prebuilt library for common platforms, so
`pip install python-snap7` is often enough. Verify with:

```bash
python -c "import snap7; snap7.client.Client(); print('Snap7 client library OK')"
```

Build from source only when that fails — typically on ARM boards or older
distributions.

## 2. Getting the sources

Snap7 is hosted at <https://sourceforge.net/projects/snap7/>. Download
`snap7-full-<version>.7z` and unpack it; the build files live under
`build/unix` and `build/windows`.

Mirror the archive inside your own network: factory hosts are usually
air-gapped, and the build machine may not be the target machine.

## 3. Linux — x86_64

```bash
sudo apt-get install -y build-essential p7zip-full
7z x snap7-full-1.4.2.7z
cd snap7-full-1.4.2/build/unix
make -f x86_64_linux.mk all

sudo cp ../bin/x86_64-linux/libsnap7.so /usr/local/lib/
sudo ldconfig
ldconfig -p | grep snap7
```

## 4. Linux — arm64 / aarch64 (Raspberry Pi 4/5, edge gateways)

### Native build on the device

```bash
sudo apt-get install -y build-essential p7zip-full
7z x snap7-full-1.4.2.7z
cd snap7-full-1.4.2/build/unix
make -f aarch64_linux.mk all
sudo cp ../bin/aarch64-linux/libsnap7.so /usr/local/lib/
sudo ldconfig
```

If your archive has no `aarch64_linux.mk`, copy `x86_64_linux.mk` and drop the
`-m64` flag; the source itself is architecture-neutral.

### Cross-compiling from x86_64

```bash
sudo apt-get install -y g++-aarch64-linux-gnu
cd snap7-full-1.4.2/build/unix
make -f aarch64_linux.mk all CC=aarch64-linux-gnu-g++
file ../bin/aarch64-linux/libsnap7.so   # expect: ARM aarch64
```

Copy the result to the target and run `ldconfig` there.

### 32-bit ARM (armv7, Pi 2/3 on a 32-bit OS)

```bash
make -f arm_v7_linux.mk all
```

Make sure the Python interpreter matches the library's word size — a 64-bit
Python cannot load a 32-bit `libsnap7.so`.

## 5. Windows — x64

Prebuilt DLLs ship with the Snap7 archive under `build/bin/win64/snap7.dll`.
Copy it next to the service's Python executable:

```
C:\Program Files\Snap7Gateway\venv\Scripts\snap7.dll
```

`install\windows\install-service.ps1` does this automatically if you place your
own `snap7.dll` at `install\windows\snap7.dll` before running it.

To build from source, open `build/windows/msvc/snap7.sln` in Visual Studio,
select **Release / x64**, and build. Use the x64 build: the service runs under
64-bit Python.

## 6. Pointing the gateway at a specific library

Standard loader rules apply.

* Linux: install into `/usr/local/lib` and run `ldconfig`, or set
  `LD_LIBRARY_PATH` in the systemd unit:

  ```ini
  Environment=LD_LIBRARY_PATH=/opt/snap7/lib
  ```

* Windows: place `snap7.dll` beside `python.exe`, or add its folder to the
  machine `PATH`.

## 7. Verifying a target host

```bash
python - <<'PY'
import snap7
print("python-snap7", snap7.__version__ if hasattr(snap7, "__version__") else "?")
snap7.client.Client()
print("client library loads")
s = snap7.server.Server(log=False)
s.start_to("127.0.0.1", tcp_port=10502); s.stop(); s.destroy()
print("server role starts")
PY
```

Then run the project's own suite, which exercises both roles against a Snap7
server simulator:

```bash
pytest tests/ -q
```

## 8. Firewall and network notes

| Port | Direction | Purpose |
| --- | --- | --- |
| 102/TCP | gateway → PLC | Snap7 client polling |
| 102/TCP | DeviceWise → gateway | Virtual S7 CPU |
| 8443/TCP | operator → gateway | Web configuration UI (HTTPS) |

Binding 102 needs `CAP_NET_BIND_SERVICE` on Linux (the supplied systemd unit
grants it) or Administrator/LocalSystem on Windows.

## 9. Reproducible deployment

The Python side is pinned exactly in `requirements.lock.txt`, which lists every
package including transitive ones. For an air-gapped site, build a wheelhouse on
a machine with network access:

```bash
# Linux target
pip download -d wheelhouse -r requirements.lock.txt

# Windows target (adds pywin32, which has no Linux wheel - download this on
# a Windows host, or pass --platform/--only-binary to pip)
pip download -d wheelhouse -r requirements-windows.txt

# copy wheelhouse/ and the source tree to the target, then:
pip install --no-index --find-links wheelhouse -r requirements.lock.txt
pip install --no-index --no-deps .
```

Regenerate the lock file whenever `requirements.txt` changes:

```bash
python3.13 -m venv /tmp/lockvenv
/tmp/lockvenv/bin/pip install -r requirements.txt
/tmp/lockvenv/bin/pip freeze > requirements.lock.txt   # keep the header comment
```

`tests/test_packaging.py` fails if the lock file falls out of step with
`requirements.txt`.

Keep the native `libsnap7.so` / `snap7.dll` you built alongside the wheelhouse,
and record its Snap7 version — it is the one component `pip` cannot restore.
