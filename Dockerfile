FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
COPY requirements.prod.txt .

ARG TORCH_VERSION=2.5.1

RUN pip install --no-cache-dir \
    --index-url https://download.pytorch.org/whl/cpu \
    "torch==${TORCH_VERSION}+cpu"

RUN pip install --no-cache-dir \
    -i https://mirrors.aliyun.com/pypi/simple \
    -r requirements.prod.txt

RUN python -m playwright install --with-deps chromium chrome

COPY app ./app
COPY scripts ./scripts
COPY config ./config

EXPOSE 8000

CMD ["sh", "-c", "python -m uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
