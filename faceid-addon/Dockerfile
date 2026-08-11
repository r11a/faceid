ARG BUILD_FROM
FROM $BUILD_FROM

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3-venv python3-dev build-essential curl ca-certificates ffmpeg \
        libglib2.0-0 libgl1 libxcb1 libgomp1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/faceid
RUN mkdir -p /opt/faceid/models \
    && curl -fL --retry 3 -o /opt/faceid/models/dinov2-small.onnx \
       https://huggingface.co/onnx-community/dinov2-small-ONNX/resolve/main/onnx/model.onnx \
    && echo "6266c3cd72db6953cecdcbfeab9422a9f783d96f1a4e296ba70ffbac43b54a18  /opt/faceid/models/dinov2-small.onnx" | sha256sum -c -
RUN curl -fL --retry 3 -o /opt/faceid/models/liveness.onnx \
       https://raw.githubusercontent.com/facenox/face-antispoof-onnx/2d4b33a3c0ba6e27772ac3a9b48ec495bf5c1dad/models/best_model_quantized.onnx \
    && echo "fde20585635cae62ed1d41796f76b6f8bc4b92cd91ec1cf0f1bc6485d2d587a9  /opt/faceid/models/liveness.onnx" | sha256sum -c -
COPY requirements.txt .
RUN python3 -m venv venv \
    && venv/bin/pip install --no-cache-dir --upgrade pip \
    && venv/bin/pip install --no-cache-dir -r requirements.txt \
    && apt-get purge -y build-essential python3-dev && apt-get autoremove -y

COPY app app
COPY static static
COPY run.sh /
RUN chmod a+x /run.sh

CMD ["/run.sh"]
