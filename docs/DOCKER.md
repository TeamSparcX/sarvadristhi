# Running Sarvadristhi in Docker

One image with everything the workbench needs on Linux:
- Python 3.12 and Sarvadristhi
- FFmpeg, Tesseract, ewf-tools and Pango
- The Sleuth Kit (pytsk3)
- PyTorch (CPU) and RF-DETR with its weights
- the YuNet and SFace face models

It runs on any machine with Docker: Windows (Docker Desktop), Linux or macOS. Nothing in it calls the internet at run time.

## 1. Get the image

**From the GitHub release** (no build needed). Download `sarvadristhi-0.1.0-docker.tar.gz` from the release, then:

```bash
docker load -i sarvadristhi-0.1.0-docker.tar.gz
```

**Or build it** from this repository (about 10 minutes):

```bash
docker build -t sarvadristhi:0.1.0 .                        # with the AI layers (CPU)
docker build -t sarvadristhi:0.1.0-core --build-arg WITH_AI=0 .   # smaller, no PyTorch
```

## 2. Put the evidence next to it

The image holds the program only; cases and disk images stay on your disk and are mounted in. You need two folders:

```
cases/   finished or new cases (e.g. OP-2013)
kit/     the seized-disk images the cases point to (e.g. kit/scenario/images/DVR-A_hikvision.img)
```

For the demo, unzip the data release into these folders, as described in `docs/RUN_A_TO_Z.md`. Cases made on Windows open as they are: paths such as `C:\...\kit\scenario\...` are found under the mounted `kit/`.

## 3. Run the workbench

```bash
docker run --rm --name sarvadristhi -p 127.0.0.1:8770:8765 \
  -v "$PWD/cases:/app/cases" -v "$PWD/kit:/app/kit" sarvadristhi:0.1.0
```

PowerShell: replace `$PWD` with `${PWD}`, and the trailing `\` with a backtick.

Open **http://127.0.0.1:8770**.

- `-p 127.0.0.1:8770:8765` publishes the port on this machine only, never on the network. Keep the `127.0.0.1:` part.
- Add `:ro` to the kit mount (`-v "$PWD/kit:/app/kit:ro"`) when you only view cases. Analysis never writes to evidence; it writes only into `cases/`.
- **Layer 3 (Qwen3-VL).** The container looks for Ollama on the host at `http://host.docker.internal:11434`. On Linux, add `--add-host=host.docker.internal:host-gateway`. Without Ollama, layer 3 reports "not reachable" and the other layers still run.

## 4. Command line inside the container

```bash
docker run --rm -v "$PWD/cases:/app/cases" -v "$PWD/kit:/app/kit" sarvadristhi:0.1.0 \
  python -m sarvadristhi verify-custody /app/cases/OP-2013
docker run --rm -v "$PWD/cases:/app/cases" -v "$PWD/kit:/app/kit" sarvadristhi:0.1.0 \
  python -m sarvadristhi replay /app/cases/OP-2013/capsule
docker run --rm -v "$PWD/kit:/app/kit" sarvadristhi:0.1.0 python -m pytest -q   # needs the test kit
```

## Limits

- **CPU only.** The AI layers run on the CPU in the container, so analysis with AI is much slower than on a CUDA laptop. Viewing finished cases is not affected.
- **No physical drives inside the container.** Image a drive on the host first (`sarvadristhi acquire`, with a write-blocker), then analyse the image.
- **Images only, in "New analysis".** The attached-drives list and the presets refer to the container's own view of the disk.
