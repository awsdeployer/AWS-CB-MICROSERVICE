# -------- Stage 1: Builder --------
ARG VERSION=3.9.18-slim
FROM python:${VERSION} AS builder
# Set working directory
WORKDIR /app
# Install build deps, AWS CLI, create appuser
RUN apt-get update && \
    apt-get install -y --no-install-recommends curl unzip ca-certificates bash git build-essential && \
    curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o "/tmp/awscliv2.zip" && \
    unzip /tmp/awscliv2.zip -d /tmp && \
    /tmp/aws/install && \
    rm -rf /tmp/aws /tmp/awscliv2.zip /var/lib/apt/lists/* && \
    useradd --system --create-home --shell /usr/sbin/nologin appuser
# Copy requirements and install Python deps into separate dir for copying later
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt
# Copy application code
COPY . .

# -------- Stage 2: Runtime --------
FROM python:${VERSION}
WORKDIR /app
# Copy all needed files from builder in one layer
COPY --from=builder /install /usr/local \
                    /usr/local/aws-cli /usr/local/aws-cli \
                    /usr/local/bin/aws /usr/local/bin/aws \
                    /etc/passwd /etc/passwd \
                    /etc/group /etc/group \
                    /app /app
# Set environment variables
ENV PATH="/usr/local/bin:/usr/local/aws-cli/v2/current/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
# Expose port and use non-root user
EXPOSE 5000
USER appuser
# Run Flask app with Gunicorn
ENTRYPOINT ["gunicorn", "--bind", "0.0.0.0:5000", "app:app"]
