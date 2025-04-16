<<<<<<< Updated upstream
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
=======
# Coreflux MCP Server

This is a Model Context Protocol (MCP) server that connects to a Coreflux MQTT broker and makes Coreflux actions available as tools for Claude and other MCP-compatible AI assistants.

## Features

- Connects to Coreflux MQTT broker 
- Provides tools for all Coreflux commands (models, actions, rules, routes)
- Discovers and lists available actions
- Includes LOT language documentation as resources
- Built with the official MCP SDK for seamless Claude integration
- Interactive setup assistant for first-time configuration
- Enhanced security with user authentication and permission management
- System health monitoring and troubleshooting tools
- Version checking and update notifications
- Secure command validation and input sanitization

## Security Notice

This server provides access to your Coreflux MQTT broker and allows execution of commands through the broker. It's important to secure your deployment:

- **Change default passwords**: Always change the default admin password immediately after setup
- **Use TLS**: Enable TLS for MQTT connections when possible
- **Set permissions**: Configure proper user roles and permissions to limit access
- **Check for updates**: Regularly update to the latest version to receive security patches

## Quick Start Guide

1. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

2. Start the server:
   ```bash
   python server.py
   ```

3. The setup assistant will guide you through initial configuration
4. Connect your MCP-compatible application (like Claude) to the server

## Setup Assistant

The server includes an interactive setup assistant that runs automatically on first launch. This assistant helps you:

- Create or update the `.env` file with your configuration
- Configure MQTT broker settings (host, port, credentials)
- Set up TLS configuration if needed
- Configure logging options

The setup assistant can be disabled by setting `ENABLE_SETUP_ASSISTANT = False` in the `server.py` file.

## Security Best Practices

### For Development Environments
- Use strong passwords for all accounts
- Limit network exposure, preferably keeping the server on localhost
- Regularly rotate logs to prevent information leakage
- Enable debug logging only when needed

### For Production Deployments
- **Always use TLS** for MQTT connections
- Configure a dedicated user with limited permissions for the MCP server
- Implement a proper firewall to restrict access to the broker
- Set up regular backups of your configuration
- Deploy in a containerized environment when possible
- Use proper certificate management for TLS connections
- Monitor logs for unauthorized access attempts

### Securing Your MQTT Broker
- Enable username/password authentication on the broker
- Configure ACLs (Access Control Lists) to limit topic access
- Disable anonymous access
- Use TLS certificates for client verification
- Configure proper firewall rules to limit access

## Connecting Claude to the MCP Server

### Using Claude Desktop Config

1. Create or edit `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS/Linux) or `%USERPROFILE%\AppData\Roaming\Claude\claude_desktop_config.json` (Windows)
2. Add the following configuration (adjust the paths accordingly):
   ```json
   {
     "mcpServers": {
       "coreflux": {
         "command": "python",
         "args": [
           "/PATH/TO/server.py",
           "--mqtt-host", "localhost", 
           "--mqtt-port", "1883",
           "--mqtt-user", "root",
           "--mqtt-password", "coreflux",
           "--mqtt-client-id", "claude-coreflux-client"
         ],
         "description": "Coreflux MQTT Broker Control",
         "icon": "🔄",
         "env": {}
       }
     }
   }
   ```
3. Restart Claude Desktop

### Command-Line Arguments

The server accepts the following command-line arguments. These settings can also be configured via the `.env` file using the setup assistant:

| Argument | Description | Default |
|----------|-------------|---------|
| `--mqtt-host` | MQTT broker address | localhost |
| `--mqtt-port` | MQTT broker port | 1883 |
| `--mqtt-user` | MQTT username | - |
| `--mqtt-password` | MQTT password | - |
| `--mqtt-client-id` | MQTT client ID | claude-mcp-client |
| `--mqtt-use-tls` | Enable TLS for MQTT connection | false |
| `--mqtt-ca-cert` | Path to CA certificate file | - |
| `--mqtt-client-cert` | Path to client certificate file | - |
| `--mqtt-client-key` | Path to client key file | - |
| `--log-level` | Logging level (DEBUG/INFO/WARNING/ERROR/CRITICAL) | INFO |

## User Management

The server includes a user authentication system with role-based permissions:

### Roles
- **admin**: Full access to all functions
- **user**: Limited access to safe functions
- **readonly**: View-only access to non-destructive functions

### Managing Users
- A default admin user is created on first run
- **IMPORTANT**: Change the default admin password immediately
- Use the `manage_user` tool to create, update, or delete users
- Tokens expire after 1 hour by default

## Available Tools

The server provides tools for common Coreflux commands:

### Coreflux Commands
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
- `request_lot_code`: Generate LOT code based on natural language prompts

### Authentication Tools
- `login`: Authenticate and get an access token
- `logout`: Invalidate the current access token
- `manage_user`: Create, update, or delete users
- `list_users`: List all registered users (admin only)

### Monitoring and Maintenance
- `health_check`: Perform a comprehensive server health check
- `get_server_info`: Get server version and status information
- `check_update`: Check for server updates
- `rotate_logs`: Rotate log files to prevent them from growing too large
- `troubleshoot`: Run diagnostics to identify common issues
- `reconnect_mqtt`: Force reconnection to the MQTT broker
- `check_broker_health`: Check MQTT broker connectivity

## Health Monitoring

The server includes built-in health monitoring capabilities:

- **System metrics**: CPU, memory, disk usage
- **MQTT connection**: Connection status, reconnection attempts
- **Log management**: Log rotation and size monitoring
- **Update checking**: Notification of new versions
- **Security scanning**: Detection of known vulnerabilities

Use the `health_check` tool for a comprehensive overview or `troubleshoot` for automated diagnostics.

## Update Management

The server includes a version checking system:

- Automatically checks for updates daily
- Notifies about available updates
- Warns about known security vulnerabilities in older versions
- Provides update URLs and release notes

Use the `check_update` tool to manually check for new versions.

## Potential Risks for Self-Hosters

When self-hosting this server, be aware of these potential risks:

1. **MQTT Command Injection**: Without proper validation, malicious commands could be sent to the broker
2. **Authentication Bypass**: Weak authentication could allow unauthorized access
3. **Sensitive Data Exposure**: Logs might contain sensitive information
4. **Denial of Service**: Resource exhaustion could affect system stability
5. **Network Exposure**: Unintended external access could lead to security breaches

### Mitigation Strategies

1. **Keep Updated**: Always run the latest version
2. **Secure Credentials**: Use strong, unique passwords
3. **Network Isolation**: Limit the server to trusted networks
4. **Regular Monitoring**: Check logs for suspicious activities
5. **Back Up Regularly**: Maintain backups of your configuration
6. **Use TLS**: Enable encryption for all connections

## Debugging and Troubleshooting

If you encounter issues:

1. Verify your MQTT broker credentials in your Claude configuration
2. Ensure the broker is accessible 
3. Run the server again with the setup assistant to verify or update your configuration
4. Use the `troubleshoot` tool to automatically diagnose common problems
5. Check Claude Desktop logs:
   ```bash
   # Check Claude's logs for errors (macOS/Linux)
   tail -n 20 -f ~/Library/Logs/Claude/mcp*.log
   # Windows PowerShell
   Get-Content -Path "$env:USERPROFILE\AppData\Roaming\Claude\Logs\mcp*.log" -Tail 20 -Wait
   ```
6. Run the server with debug logging:
   ```bash
   # Direct execution with debug logging
   python server.py --mqtt-host localhost --mqtt-port 1883 --log-level DEBUG
   ```
7. Check the server's logs in `coreflux_mcp.log`

## Contributing

We welcome contributions to improve the Coreflux MCP Server! Please follow these guidelines:

### Security Guidelines
- All contributions must pass security review
- No hardcoded credentials or secrets
- Proper input validation for all user inputs 
- All network connections must be secured
- Errors must be logged appropriately without leaking sensitive data

### Pull Request Process
1. Fork the repository
2. Create a feature branch
3. Add/update tests for any new functionality
4. Ensure all security checks pass
5. Submit a PR with detailed description
6. Security review must pass before merging

## References

- [MCP Quickstart for Server Developers](https://modelcontextprotocol.io/quickstart/server)
- [MCP Official Documentation](https://modelcontextprotocol.io/)
- [MQTT Security Fundamentals](https://www.hivemq.com/mqtt-security-fundamentals/)
- [Coreflux Documentation](https://coreflux.org/docs/)
>>>>>>> Stashed changes
