FROM python:3.10-slim

WORKDIR /app

# Install dependencies using the official MCP SDK and latest paho-mqtt
RUN pip install --no-cache-dir "mcp[cli]>=1.2.0" paho-mqtt>=2.0.0

# Copy application files
COPY server.py .

# Expose port for MCP server
EXPOSE 8000

# Environment variables with defaults
ENV MQTT_BROKER=localhost \
    MQTT_PORT=1883 \
    MQTT_USE_TLS=false \
    MQTT_CLIENT_ID=""

# Set command to run the MCP server with CMD array for better argument handling
# These environment variables will be used as defaults if no arguments are provided
ENTRYPOINT ["python", "server.py"]
CMD ["--mqtt-host", "${MQTT_BROKER}", "--mqtt-port", "${MQTT_PORT}", "--mqtt-client-id", "${MQTT_CLIENT_ID}"] 