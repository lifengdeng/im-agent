ARG BASE_IMAGE
FROM ${BASE_IMAGE}
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /candidate
COPY im_agent im_agent
COPY tests/test_runtime.py tests/test_runtime.py
COPY tests/test_offline_network.py tests/test_offline_network.py
COPY tests/test_adapters.py tests/test_adapters.py
COPY tests/test_bundles.py tests/test_bundles.py
COPY tests/test_codex.py tests/test_codex.py
COPY tests/test_resident.py tests/test_resident.py
COPY tests/fixtures tests/fixtures
USER 10001:10001
CMD ["python", "-B", "-m", "im_agent.validation"]
