# Sarvadristhi workbench in a container (Linux, CPU).
#
#   docker build -t sarvadristhi:0.1.0 .
#   docker run --rm -p 127.0.0.1:8770:8765 -v ./cases:/app/cases -v ./kit:/app/kit sarvadristhi:0.1.0
#   -> http://127.0.0.1:8770
#
# The UI listens on 0.0.0.0 inside the container only so Docker can forward it; publish it on
# 127.0.0.1 as above and it stays local to this machine. Evidence is opened read-only; mount
# kit/ with :ro if you only view cases. Build without the AI layers: --build-arg WITH_AI=0.
FROM python:3.12-slim-bookworm

ARG WITH_AI=1
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 \
    SARVADRISTHI_OLLAMA=http://host.docker.internal:11434

# FFmpeg (decode/export), Tesseract (overlay OCR), ewf-tools (E01 acquire/verify), Pango (PDF reports)
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg tesseract-ocr tesseract-ocr-eng ewf-tools \
        libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz-subset0 fonts-dejavu-core \
        curl ca-certificates build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY sarvadristhi sarvadristhi
COPY pramanshala pramanshala

# editable install: the code finds models/ and kit/ next to the package, as on Windows
RUN pip install -e ".[pdf,test]" pytsk3 \
    && if [ "$WITH_AI" = "1" ]; then \
         pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu && pip install rfdetr; \
       fi \
    && apt-get purge -y build-essential && apt-get autoremove -y

# face models (OpenCV Zoo, Apache-2.0); RF-DETR weights fetched now so analysis runs offline
RUN mkdir -p models \
    && curl -fsSL -o models/face_detection_yunet_2023mar.onnx \
       https://media.githubusercontent.com/media/opencv/opencv_zoo/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx \
    && curl -fsSL -o models/face_recognition_sface_2021dec.onnx \
       https://media.githubusercontent.com/media/opencv/opencv_zoo/main/models/face_recognition_sface/face_recognition_sface_2021dec.onnx \
    && if [ "$WITH_AI" = "1" ]; then python -c "from rfdetr import RFDETRBase; RFDETRBase()"; fi

COPY tests tests

VOLUME ["/app/cases", "/app/kit"]
EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s CMD curl -fs http://127.0.0.1:8765/ >/dev/null || exit 1
CMD ["python", "-m", "sarvadristhi", "ui", "--cases", "/app/cases", "--host", "0.0.0.0", "--port", "8765", "--no-browser"]
