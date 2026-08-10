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
