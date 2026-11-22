FROM python:3.12-slim

# bisect and the run manifest call the git binary.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir .

# Mount a numblame project here: docker run --rm -v "$PWD:/project" numblame run
WORKDIR /project
ENV NUMBLAME_PROJECT=/project

# Mounted repositories are owned by another user; let git read them.
RUN git config --system --add safe.directory /project

ENTRYPOINT ["numblame"]
CMD ["--help"]
