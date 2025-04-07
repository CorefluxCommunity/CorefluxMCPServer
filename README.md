# Coreflux MCP Server

This is a Model Context Protocol (MCP) server that connects to a Coreflux MQTT broker and makes Coreflux actions available as tools for Claude and other MCP-compatible AI assistants.

## Features

- Connects to Coreflux MQTT broker 
- Provides tools for all Coreflux commands (models, actions, rules, routes)
- Discovers and lists available actions
- Includes LOT language documentation as resources
- Built with the official MCP SDK for seamless Claude integration

## Quick Start

### Using Docker Compose (Recommended)

1. Copy the example environment file:
   ```bash
   cp .env.example .env
   ```

2. Edit the `.env` file with your MQTT broker details:
   ```
   MQTT_BROKER=your-broker-address
   MQTT_PORT=1883
   MQTT_USER=your-username
   MQTT_PASSWORD=your-password
   ```

3. Run with docker-compose:
   ```bash
   docker-compose up -d
   ```

### Using Python (Local Development)

1. Install the MCP SDK and required dependencies:
   ```bash
   pip install "mcp[cli]" paho-mqtt
   ```

2. Run the server with environment variables:
   ```bash
   MQTT_BROKER=your-broker-address MQTT_PORT=1883 python server.py
   ```

## Connecting Claude to the MCP Server

### Recommended Approach: Using Claude Desktop Config

1. Create or edit `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS/Linux) or equivalent Windows path
2. Add the following configuration (adjust the paths accordingly):
   ```json
   {
     "mcpServers": {
       "coreflux": {
         "command": "python",
         "args": [
           "/ABSOLUTE/PATH/TO/server.py"
         ],
         "description": "Coreflux MQTT Broker Control",
         "icon": "🔄",
         "env": {
           "MQTT_BROKER": "localhost",
           "MQTT_PORT": "1883",
           "MQTT_USER": "",
           "MQTT_PASSWORD": ""
         }
       }
     }
   }
   ```
3. Restart Claude Desktop

### Alternative: Using the CLI

If you're running the server directly:

```bash
mcp server add coreflux --command python --args "/ABSOLUTE/PATH/TO/server.py"
```

## Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `MQTT_BROKER` | MQTT broker address | localhost |
| `MQTT_PORT` | MQTT broker port | 1883 |
| `MQTT_USER` | MQTT username | - |
| `MQTT_PASSWORD` | MQTT password | - |
| `MQTT_USE_TLS` | Enable TLS for MQTT connection | false |
| `MQTT_CA_CERT` | Path to CA certificate file | - |
| `MQTT_CLIENT_CERT` | Path to client certificate file | - |
| `MQTT_CLIENT_KEY` | Path to client key file | - |

## Available Tools

The server provides tools for common Coreflux commands:

- `add_rule`: Add a new permission rule
- `remove_rule`: Remove a permission rule
- `add_route`: Add a new route connection
- `remove_route`: Remove a route connection
- `add_model`: Add a new model structure
- `remove_model`: Remove a model structure
- `add_action`: Add a new action event/function
- `remove_action`: Remove an action event/function
- `run_action`: Run an action event/function
- `remove_all_models`: Remove all models
- `remove_all_actions`: Remove all actions
- `remove_all_routes`: Remove all routes
- `list_discovered_actions`: List all discovered Coreflux actions

## LOT Language Resources

The server includes documentation on the LOT language:

- `lot://documentation/models`: Guide to LOT models
- `lot://documentation/rules`: Guide to LOT rules
- `lot://documentation/actions`: Guide to LOT actions

## Debugging and Troubleshooting

If you encounter issues:

1. Verify your MQTT broker credentials in your environment variables
2. Ensure the broker is accessible 
3. Check Claude Desktop logs:
   ```bash
   # Check Claude's logs for errors (macOS/Linux)
   tail -n 20 -f ~/Library/Logs/Claude/mcp*.log
   ```
4. Run the server in debug mode:
   ```bash
   # Direct execution with debug logging
   python server.py --debug
   ```

## References

- [MCP Quickstart for Server Developers](https://modelcontextprotocol.io/quickstart/server)
- [MCP Official Documentation](https://modelcontextprotocol.io/) 