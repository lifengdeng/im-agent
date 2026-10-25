ARG BASE_IMAGE
FROM ${BASE_IMAGE}
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements-feishu.lock /tmp/requirements-feishu.lock
RUN python -m pip install --no-cache-dir --require-hashes -r /tmp/requirements-feishu.lock
COPY . /app
USER 10001:10001
ENTRYPOINT ["python", "-B", "scripts/digital_twin.py"]
