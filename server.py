<<<<<<< Updated upstream
from mcp.server.fastmcp import FastMCP, Context
import os
import paho.mqtt.client as mqtt
import uuid
import argparse
import requests
import json
import logging
from logging.handlers import RotatingFileHandler
import time
import sys
from typing import Dict, Any, Optional

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        RotatingFileHandler('coreflux_mcp.log', maxBytes=10485760, backupCount=5)  # 10MB per file, 5 backups
    ]
)
logger = logging.getLogger('coreflux_mcp')

# Parse command-line arguments
def parse_args():
    parser = argparse.ArgumentParser(description="Coreflux MCP Server")
    parser.add_argument("--mqtt-host", default=os.environ.get("MQTT_BROKER", "localhost"),
                      help="MQTT broker hostname")
    parser.add_argument("--mqtt-port", type=int, default=int(os.environ.get("MQTT_PORT", "1883")),
                      help="MQTT broker port")
    parser.add_argument("--mqtt-user", default=os.environ.get("MQTT_USER"),
                      help="MQTT username")
    parser.add_argument("--mqtt-password", default=os.environ.get("MQTT_PASSWORD"),
                      help="MQTT password")
    parser.add_argument("--mqtt-client-id", default=os.environ.get("MQTT_CLIENT_ID", f"coreflux-mcp-{uuid.uuid4().hex[:8]}"),
                      help="MQTT client ID")
    parser.add_argument("--mqtt-use-tls", action="store_true", default=os.environ.get("MQTT_USE_TLS", "false").lower() == "true",
                      help="Use TLS for MQTT connection")
    parser.add_argument("--mqtt-ca-cert", default=os.environ.get("MQTT_CA_CERT"),
                      help="Path to CA certificate file for TLS")
    parser.add_argument("--mqtt-client-cert", default=os.environ.get("MQTT_CLIENT_CERT"),
                      help="Path to client certificate file for TLS")
    parser.add_argument("--mqtt-client-key", default=os.environ.get("MQTT_CLIENT_KEY"),
                      help="Path to client key file for TLS")
    parser.add_argument("--log-level", default=os.environ.get("LOG_LEVEL", "INFO"),
                      choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
                      help="Set logging level")
    return parser.parse_args()

# Configure FastMCP server
mcp = FastMCP(
    "Coreflux Broker",
    description="Connect to a Coreflux MQTT broker and control Coreflux actions, models, and rules",
    dependencies=["paho-mqtt"]
)

# Global MQTT client
mqtt_client = None
discovered_actions = {}
registered_dynamic_tools = set()  # Keep track of dynamically registered tools
mqtt_connection_status = {
    "connected": False,
    "last_error": None,
    "last_attempt": None,
    "reconnect_count": 0
}

# MQTT connection and message handling
def on_connect(client, userdata, flags, rc, properties=None):
    """Callback for when the client connects to the MQTT broker."""
    rc_codes = {
        0: "Connection successful",
        1: "Connection refused - incorrect protocol version",
        2: "Connection refused - invalid client identifier",
        3: "Connection refused - server unavailable",
        4: "Connection refused - bad username or password",
        5: "Connection refused - not authorised"
    }
    
    message = rc_codes.get(rc, f"Unknown result code: {rc}")
    
    if rc == 0:
        mqtt_connection_status["connected"] = True
        mqtt_connection_status["last_error"] = None
        logger.info(f"Connected to MQTT broker: {message}")
        
        # Subscribe to all action descriptions
        try:
            result, mid = client.subscribe("$SYS/Coreflux/Actions/+/Description")
            if result == mqtt.MQTT_ERR_SUCCESS:
                logger.info("Successfully subscribed to action descriptions")
            else:
                logger.error(f"Failed to subscribe to action descriptions: {result}")
        except Exception as e:
            logger.error(f"Error subscribing to topics: {str(e)}")
    else:
        mqtt_connection_status["connected"] = False
        mqtt_connection_status["last_error"] = message
        logger.error(f"Failed to connect to MQTT broker: {message}")

def on_disconnect(client, userdata, rc, properties=None):
    """Callback for when the client disconnects from the MQTT broker."""
    mqtt_connection_status["connected"] = False
    if rc == 0:
        logger.info("Disconnected from MQTT broker successfully")
    else:
        mqtt_connection_status["last_error"] = "Unexpected disconnection"
        logger.warning(f"Unexpected disconnection from MQTT broker, rc={rc}")
        mqtt_connection_status["reconnect_count"] += 1

def on_message(client, userdata, msg):
    """Callback for when a message is received from the MQTT broker."""
    try:
        # Extract action name from topic
        topic_parts = msg.topic.split('/')
        if len(topic_parts) >= 4 and topic_parts[-1] == "Description":
            action_name = topic_parts[-2]
            description = msg.payload.decode('utf-8')
            
            # Check if we already have this action
            if action_name in discovered_actions:
                # Only update the description if it changed
                if discovered_actions[action_name] != description:
                    discovered_actions[action_name] = description
                    logger.info(f"Updated action description: {action_name} - {description}")
                return
                
            # New action discovered
            discovered_actions[action_name] = description
            logger.info(f"Discovered new action: {action_name} - {description}")
            
            # Register a dynamic tool for this action if not already registered
            if action_name not in registered_dynamic_tools:
                register_dynamic_action_tool(action_name, description)
    except Exception as e:
        logger.error(f"Error processing message from topic {msg.topic}: {str(e)}")

def on_publish(client, userdata, mid):
    """Callback for when a message is published."""
    logger.debug(f"Message {mid} published successfully")

def register_dynamic_action_tool(action_name, description):
    """Register a dynamic tool for a discovered action."""
    try:
        # Skip if already registered
        if action_name in registered_dynamic_tools:
            return
            
        # Create a unique function name for this action
        tool_func_name = f"run_{action_name}"
        
        # Escape any quotes in the description to avoid syntax errors
        escaped_description = description.replace('"', '\\"').replace("'", "\\'")
        
        # We need to create a function with a dynamic name
        # This approach uses exec to create a function with the exact name we want
        exec(f"""
@mcp.tool()
async def {tool_func_name}(ctx: Context) -> str:
    \"\"\"Run the {action_name} action: {escaped_description}\"\"\"
    return execute_command(f"-runAction {action_name}")
""", globals())
        
        # Mark as registered
        registered_dynamic_tools.add(action_name)
        logger.info(f"Registered dynamic tool for action: {action_name} as {tool_func_name}")
    except Exception as e:
        logger.error(f"Error registering dynamic tool for action {action_name}: {str(e)}")

# Setup MQTT client
def setup_mqtt(args):
    """Set up the MQTT client with the provided arguments."""
    global mqtt_client
    
    try:
        # Use protocol version 5 (MQTT v5) with the newer callback API and unique client ID
        mqtt_client = mqtt.Client(client_id=args.mqtt_client_id, protocol=mqtt.MQTTv5)
        
        # Set up authentication if provided
        if args.mqtt_user and args.mqtt_password:
            mqtt_client.username_pw_set(args.mqtt_user, args.mqtt_password)
        
        # Configure TLS if enabled
        if args.mqtt_use_tls:
            if not all([args.mqtt_ca_cert, args.mqtt_client_cert, args.mqtt_client_key]):
                logger.warning("TLS enabled but some certificate files are missing")
            
            try:
                mqtt_client.tls_set(
                    ca_certs=args.mqtt_ca_cert,
                    certfile=args.mqtt_client_cert,
                    keyfile=args.mqtt_client_key
                )
                logger.info("TLS configuration applied")
            except Exception as e:
                logger.error(f"Error configuring TLS: {str(e)}")
                return False
        
        # Set callbacks
        mqtt_client.on_connect = on_connect
        mqtt_client.on_message = on_message
        mqtt_client.on_disconnect = on_disconnect
        mqtt_client.on_publish = on_publish
        
        # Connect to broker
        mqtt_connection_status["last_attempt"] = time.time()
        logger.info(f"Connecting to MQTT broker at {args.mqtt_host}:{args.mqtt_port} with client ID: {args.mqtt_client_id}")
        
        mqtt_client.connect(args.mqtt_host, args.mqtt_port, 60)
        mqtt_client.loop_start()
        
        # Short wait to check if connection was successful
        time.sleep(1)
        if mqtt_connection_status["connected"]:
            logger.info("MQTT client connection confirmed")
            return True
        else:
            logger.error(f"MQTT connection failed: {mqtt_connection_status['last_error']}")
            return False
    except Exception as e:
        mqtt_connection_status["last_error"] = str(e)
        logger.error(f"Error connecting to MQTT broker: {str(e)}")
        return False

def mqtt_error_description(result_code):
    """Return a descriptive message for MQTT error codes."""
    error_codes = {
        mqtt.MQTT_ERR_AGAIN: "The client is currently busy, try again later",
        mqtt.MQTT_ERR_NOMEM: "Out of memory condition",
        mqtt.MQTT_ERR_PROTOCOL: "Protocol error",
        mqtt.MQTT_ERR_INVAL: "Invalid parameters",
        mqtt.MQTT_ERR_NO_CONN: "No connection to broker",
        mqtt.MQTT_ERR_CONN_REFUSED: "Connection refused",
        mqtt.MQTT_ERR_NOT_FOUND: "Topic or subscription not found",
        mqtt.MQTT_ERR_CONN_LOST: "Connection lost",
        mqtt.MQTT_ERR_TLS: "TLS error",
        mqtt.MQTT_ERR_PAYLOAD_SIZE: "Payload size too large",
        mqtt.MQTT_ERR_NOT_SUPPORTED: "Feature not supported",
        mqtt.MQTT_ERR_AUTH: "Authentication failed",
        mqtt.MQTT_ERR_ACL_DENIED: "Access denied",
        mqtt.MQTT_ERR_UNKNOWN: "Unknown error",
        mqtt.MQTT_ERR_ERRNO: "System error"
    }
    return error_codes.get(result_code, f"Unknown error code: {result_code}")

def setup_mqtt_with_retry(args, max_retries=5, retry_interval=5):
    """Set up MQTT with automatic retry."""
    for attempt in range(max_retries):
        if setup_mqtt(args):
            return True
        logger.warning(f"Connection attempt {attempt+1}/{max_retries} failed, retrying in {retry_interval}s")
        time.sleep(retry_interval)
    return False

# Helper function to execute Coreflux commands
def execute_command(command_string):
    """Execute a Coreflux command by publishing to the command topic."""
    if not mqtt_client:
        error_msg = "ERROR: MQTT client not initialized"
        logger.error(error_msg)
        return error_msg
    
    if not mqtt_connection_status["connected"]:
        error_msg = f"ERROR: MQTT client not connected. Last error: {mqtt_connection_status['last_error']}"
        logger.error(error_msg)
        return error_msg
    
    try:
        result, mid = mqtt_client.publish("$SYS/Coreflux/Command", command_string, qos=1)
        if result == mqtt.MQTT_ERR_SUCCESS:
            logger.info(f"Command published: {command_string}")
            return f"Command published: {command_string}"
        else:
            error_msg = f"Error publishing command: {result}"
            logger.error(error_msg)
            return error_msg
    except Exception as e:
        error_msg = f"Error executing command: {str(e)}"
        logger.error(error_msg)
        return error_msg

# Tools for Coreflux commands
@mcp.tool()
async def add_rule(rule_definition: str, ctx: Context) -> str:
    """
    Add a new permission rule to Coreflux
    
    Args:
        rule_definition: The LOT rule definition (DEFINE RULE...)
    """
    try:
        return execute_command(f"-addRule {rule_definition}")
    except Exception as e:
        error_msg = f"Error adding rule: {str(e)}"
        logger.error(error_msg)
        return error_msg

@mcp.tool()
async def remove_rule(rule_name: str, ctx: Context) -> str:
    """Remove a permission rule from Coreflux"""
    try:
        return execute_command(f"-removeRule {rule_name}")
    except Exception as e:
        error_msg = f"Error removing rule: {str(e)}"
        logger.error(error_msg)
        return error_msg

@mcp.tool()
async def add_route(ctx: Context) -> str:
    """Add a new route connection"""
    try:
        return execute_command("-addRoute")
    except Exception as e:
        error_msg = f"Error adding route: {str(e)}"
        logger.error(error_msg)
        return error_msg

@mcp.tool()
async def remove_route(route_id: str, ctx: Context) -> str:
    """Remove a route connection"""
    try:
        return execute_command(f"-removeRoute {route_id}")
    except Exception as e:
        error_msg = f"Error removing route: {str(e)}"
        logger.error(error_msg)
        return error_msg

@mcp.tool()
async def add_model(model_definition: str, ctx: Context) -> str:
    """
    Add a new model structure to Coreflux
    
    Args:
        model_definition: The LOT model definition (DEFINE MODEL...)
    """
    try:
        return execute_command(f"-addModel {model_definition}")
    except Exception as e:
        error_msg = f"Error adding model: {str(e)}"
        logger.error(error_msg)
        return error_msg

@mcp.tool()
async def remove_model(model_name: str, ctx: Context) -> str:
    """Remove a model structure from Coreflux"""
    try:
        return execute_command(f"-removeModel {model_name}")
    except Exception as e:
        error_msg = f"Error removing model: {str(e)}"
        logger.error(error_msg)
        return error_msg

@mcp.tool()
async def add_action(action_definition: str, ctx: Context) -> str:
    """
    Add a new action event/function to Coreflux
    
    Args:
        action_definition: The LOT action definition (DEFINE ACTION...)
    """
    try:
        return execute_command(f"-addAction {action_definition}")
    except Exception as e:
        error_msg = f"Error adding action: {str(e)}"
        logger.error(error_msg)
        return error_msg

@mcp.tool()
async def remove_action(action_name: str, ctx: Context) -> str:
    """Remove an action event/function from Coreflux"""
    try:
        return execute_command(f"-removeAction {action_name}")
    except Exception as e:
        error_msg = f"Error removing action: {str(e)}"
        logger.error(error_msg)
        return error_msg

@mcp.tool()
async def run_action(action_name: str, ctx: Context) -> str:
    """Run an action event/function in Coreflux"""
    correlation_id = str(uuid.uuid4())
    try:
        logger.info(f"[{correlation_id}] Running action: {action_name}")
        result = execute_command(f"-runAction {action_name}")
        logger.info(f"[{correlation_id}] Result: {result}")
        return result
    except Exception as e:
        error_msg = f"Error running action: {str(e)}"
        logger.error(f"[{correlation_id}] {error_msg}")
        return error_msg

@mcp.tool()
async def remove_all_models(ctx: Context) -> str:
    """Remove all models from Coreflux"""
    try:
        return execute_command("-removeAllModels")
    except Exception as e:
        error_msg = f"Error removing all models: {str(e)}"
        logger.error(error_msg)
        return error_msg

@mcp.tool()
async def remove_all_actions(ctx: Context) -> str:
    """Remove all actions from Coreflux"""
    try:
        return execute_command("-removeAllActions")
    except Exception as e:
        error_msg = f"Error removing all actions: {str(e)}"
        logger.error(error_msg)
        return error_msg

@mcp.tool()
async def remove_all_routes(ctx: Context) -> str:
    """Remove all routes from Coreflux"""
    try:
        return execute_command("-removeAllRoutes")
    except Exception as e:
        error_msg = f"Error removing all routes: {str(e)}"
        logger.error(error_msg)
        return error_msg

@mcp.tool()
async def lot_diagnostic(diagnostic_value: str, ctx: Context) -> str:
    """Change the LOT Diagnostic"""
    try:
        return execute_command(f"-lotDiagnostic {diagnostic_value}")
    except Exception as e:
        error_msg = f"Error setting LOT diagnostic: {str(e)}"
        logger.error(error_msg)
        return error_msg

@mcp.tool()
async def list_discovered_actions(ctx: Context) -> str:
    """List all discovered Coreflux actions"""
    try:
        if not discovered_actions:
            return "No actions discovered yet."
        
        result = "Discovered Coreflux Actions:\n\n"
        for action_name, description in discovered_actions.items():
            tool_status = "✓" if action_name in registered_dynamic_tools else "✗"
            result += f"- {action_name}: {description} [Tool: {tool_status}]\n"
        
        return result
    except Exception as e:
        error_msg = f"Error listing discovered actions: {str(e)}"
        logger.error(error_msg)
        return error_msg

@mcp.tool()
async def check_mqtt_status(ctx: Context) -> str:
    """Check the status of the MQTT connection and related services"""
    try:
        status = {
            "status": "ok" if mqtt_connection_status["connected"] else "error",
            "mqtt_connected": mqtt_connection_status["connected"],
            "last_error": mqtt_connection_status["last_error"],
            "last_connection_attempt": mqtt_connection_status["last_attempt"],
            "reconnect_count": mqtt_connection_status["reconnect_count"],
            "discovered_actions_count": len(discovered_actions),
            "registered_tools_count": len(registered_dynamic_tools)
        }
        
        return json.dumps(status, indent=2)
    except Exception as e:
        error_msg = {"status": "error", "message": f"Error checking MQTT status: {str(e)}"}
        logger.error(error_msg["message"])
        return json.dumps(error_msg)

@mcp.tool()
def request_lot_code(ctx: Context, query: str, context: str = "") -> str:
    """
    Request Lot code generation or Lot Knowledge (models, actions, rules) based on a natural language prompt.
    So you are able to create models , actions and rules before adding them. Any 
    logic that you need to implement in the Coreflux MQTT broker you should ask in this tool first. 
    Args:
        query: describe what the user wants in a structured way
        context: Additional context or specific requirements (optional)
    
    Returns:
        str: The reply with documentation and LOT code with the potential actions, models, rules  or routes.
    """
    api_url = "https://anselmo.coreflux.org/webhook/chat_lot_beta"
    
    payload = {
        "query": query,
        "context": context
    }
    
    try:
        logger.info(f"Making API request to LOT code generator with query: {query}")
        response = requests.post(api_url, json=payload, timeout=30)
        
        if response.status_code == 200:
            result = response.json()
            logger.info("API request successful")
            return json.dumps(result, indent=2)
        else:
            error_msg = f"Error: API request failed with status {response.status_code}"
            logger.error(error_msg)
            return error_msg
    except requests.exceptions.Timeout:
        error_msg = "Error: API request timed out"
        logger.error(error_msg)
        return error_msg
    except requests.exceptions.ConnectionError:
        error_msg = "Error: Unable to connect to API server"
        logger.error(error_msg)
        return error_msg
    except Exception as e:
        error_msg = f"Error making API request: {str(e)}"
        logger.error(error_msg)
        return error_msg

# Resources for LOT language documentation
@mcp.resource("lot://documentation/models")
def lot_models_docs() -> str:
    """Documentation for LOT Models"""
    return """
# LOT Language - Model Management Documentation

## 1. Overview
Models in Coreflux use the LOT language syntax to define how data is processed, transformed, and published. Models take input data (triggered by specific topics), process it through expressions, constants, or transformations, and output the results to new MQTT topics.

## 2. Model Syntax
```
DEFINE MODEL <model_name> WITH TOPIC "<output_base_topic>"
    ADD "<property_name>" WITH TOPIC "<input_topic>" [AS TRIGGER]
    ADD "<property_name>" WITH <constant_value>
    ADD "<property_name>" WITH (expression)
```

## 3. Example Model
```
DEFINE MODEL GenericEnergyCost WITH TOPIC "Coreflux/+/+/+/+/energy"
    ADD "total_energy" WITH TOPIC "shellies/+/+/+/+/device/energy" AS TRIGGER
    ADD "energy_price" WITH 3
    ADD "cost" WITH (total_energy * energy_price)
```
"""

@mcp.resource("lot://documentation/rules")
def lot_rules_docs() -> str:
    """Documentation for LOT Rules"""
    return """
# LOT Language - Rule Management Documentation

## 1. Overview
Rules in Coreflux govern user permissions and system actions, ensuring precise control over system operations.

## 2. Rule Syntax
```
DEFINE RULE <rule_name> WITH PRIORITY <priority_value> FOR <action_scope>
    IF <condition> THEN
        ALLOW
    ELSE
        DENY
```

## 3. Example Rule
```
DEFINE RULE SpecificTopicClient WITH PRIORITY 1 FOR Subscribe TO TOPIC "Emanuel/#"
    IF USER IS "Emanuel" THEN
        ALLOW
    ELSE
        DENY
```
"""

@mcp.resource("lot://documentation/actions")
def lot_actions_docs() -> str:
    """Documentation for LOT Actions"""
    return """
# LOT Language - Action Management Documentation

## 1. Overview
LOT scripting language defines Actions—small logic blocks that react to events (time-based or topic-based) and publish data to topics.

## 2. Action Syntax
```
DEFINE ACTION <ActionName>
ON EVERY ... or ON TOPIC ... or only DO 
DO
    IF <expression> THEN
        PUBLISH ...
    ELSE
        PUBLISH ...
```


## 3. Example Action that runs every 5 seconds 
```
DEFINE ACTION StrokeGenerator
ON EVERY 5 SECONDS 
DO
    IF GET TOPIC "Coreflux/Porto/MeetingRoom/Light1/command/switch:0" == "off" THEN
        PUBLISH TOPIC "Coreflux/Porto/MeetingRoom/Light1/command/switch:0" WITH "on"
    ELSE
        PUBLISH TOPIC "Coreflux/Porto/MeetingRoom/Light1/command/switch:0" WITH "off"
```
## 4. Example Action that can be called by run action 
```
DEFINE ACTION TurnLampOff
DO
    PUBLISH TOPIC "Coreflux/Porto/MeetingRoom/Light1/command/switch:0" WITH "off"
DESCRIPTION "Turns a specific topic off"
```
"""

if __name__ == "__main__":
    # Parse command-line arguments
    args = parse_args()
    
    # Set log level from arguments
    logger.setLevel(getattr(logging, args.log_level))
    logger.info(f"Starting Coreflux MCP Server with log level: {args.log_level}")
    
    # Initialize MQTT connection
    if not setup_mqtt_with_retry(args):
        logger.error("Failed to initialize MQTT connection, continuing with limited functionality")
    
    # Run with standard transport
    try:
        logger.info("Starting FastMCP server")
        mcp.run()
    except Exception as e:
        logger.critical(f"Error running FastMCP server: {str(e)}")
=======
from mcp.server.fastmcp import FastMCP, Context
import os
import paho.mqtt.client as mqtt
import uuid
import argparse
import requests
import json
import logging
import sys
import time
from datetime import datetime
from dotenv import load_dotenv
import hashlib
import secrets
import time
import json
from typing import Dict, List, Optional
import pkg_resources
import re
import threading
from urllib.parse import urlparse
import psutil
import platform
import socket
from datetime import timedelta
import traceback
import inspect
from functools import wraps

# Flag to enable/disable setup assistant
ENABLE_SETUP_ASSISTANT = True

# Load environment variables from .env file if it exists
load_dotenv()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("coreflux_mcp.log"),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger("CorefluxMCP")

# Server version information
SERVER_VERSION = "1.0.0"  # Update this when releasing new versions
VERSION_CHECK_INTERVAL = 86400  # Check for updates once per day (in seconds)
GITHUB_REPO_URL = "https://github.com/coreflux/CorefluxMCPServer"
GITHUB_API_URL = "https://api.github.com/repos/coreflux/CorefluxMCPServer/releases/latest"
UPDATE_AVAILABLE = False
LATEST_VERSION = None
VULNERABILITIES = {
    # Known vulnerabilities in older versions
    # Format: "version": "vulnerability description"
    "0.9.0": "Authentication bypass in command processing",
    "0.8.5": "Input validation vulnerability in action handling",
}

# Parse command-line arguments
def parse_args():
    parser = argparse.ArgumentParser(description="Coreflux MCP Server")
    parser.add_argument("--mqtt-host", default=os.environ.get("MQTT_BROKER", "localhost"),
                      help="MQTT broker hostname")
    parser.add_argument("--mqtt-port", type=int, default=int(os.environ.get("MQTT_PORT", "1883")),
                      help="MQTT broker port")
    parser.add_argument("--mqtt-user", default=os.environ.get("MQTT_USER"),
                      help="MQTT username")
    parser.add_argument("--mqtt-password", default=os.environ.get("MQTT_PASSWORD"),
                      help="MQTT password")
    parser.add_argument("--mqtt-client-id", default=os.environ.get("MQTT_CLIENT_ID", f"coreflux-mcp-{uuid.uuid4().hex[:8]}"),
                      help="MQTT client ID")
    parser.add_argument("--mqtt-use-tls", action="store_true", default=os.environ.get("MQTT_USE_TLS", "false").lower() == "true",
                      help="Use TLS for MQTT connection")
    parser.add_argument("--mqtt-ca-cert", default=os.environ.get("MQTT_CA_CERT"),
                      help="Path to CA certificate file for TLS")
    parser.add_argument("--mqtt-client-cert", default=os.environ.get("MQTT_CLIENT_CERT"),
                      help="Path to client certificate file for TLS")
    parser.add_argument("--mqtt-client-key", default=os.environ.get("MQTT_CLIENT_KEY"),
                      help="Path to client key file for TLS")
    parser.add_argument("--log-level", default=os.environ.get("LOG_LEVEL", "INFO"),
                      choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
                      help="Set logging level")
    return parser.parse_args()

# Configure FastMCP server
mcp = FastMCP(
    "Coreflux Broker",
    description="Connect to a Coreflux MQTT broker and control Coreflux actions, models, and rules",
    dependencies=["paho-mqtt"]
)

# Global MQTT client
mqtt_client = None
discovered_actions = {}
registered_dynamic_tools = set()  # Keep track of dynamically registered tools
connection_status = {
    "connected": False,
    "last_connection_attempt": None,
    "reconnect_count": 0,
    "last_error": None
}
server_start_time = datetime.now()

# Global variable for permission management
user_auth = {
    "users": {},
    "tokens": {},
    "permissions": {}
}

# Constants for authentication
AUTH_TOKEN_EXPIRY = 3600  # 1 hour in seconds
DEFAULT_ADMIN_PASSWORD = "admin"  # Should be changed at first run

# Default permission configuration
DEFAULT_PERMISSIONS = {
    "admin": ["*"],  # Admin has all permissions
    "user": [
        "list_discovered_actions",
        "get_connection_status",
        "check_broker_health",
        "run_action"
    ]
}

# User roles
USER_ROLES = ["admin", "user", "readonly"]

# MQTT connection and message handling
def on_connect(client, userdata, flags, rc, properties=None):
    result_code_map = {
        0: "Connection successful",
        1: "Connection refused - incorrect protocol version",
        2: "Connection refused - invalid client identifier",
        3: "Connection refused - server unavailable",
        4: "Connection refused - bad username or password",
        5: "Connection refused - not authorized"
    }
    
    if rc == 0:
        connection_status["connected"] = True
        connection_status["reconnect_count"] = 0
        connection_status["last_error"] = None
        logger.info(f"Connected to MQTT broker successfully (code: {rc})")
        
        # Subscribe to all action descriptions
        try:
            client.subscribe("$SYS/Coreflux/Actions/+/Description")
            logger.info("Subscribed to Coreflux action descriptions")
        except Exception as e:
            logger.error(f"Failed to subscribe to topics: {str(e)}")
    else:
        connection_status["connected"] = False
        connection_status["last_error"] = result_code_map.get(rc, f"Unknown error code: {rc}")
        logger.error(f"Failed to connect to MQTT broker: {connection_status['last_error']}")

def on_disconnect(client, userdata, rc, properties=None):
    connection_status["connected"] = False
    if rc == 0:
        logger.info("Disconnected from MQTT broker gracefully")
    else:
        logger.warning(f"Unexpected disconnection from MQTT broker (code: {rc})")
        # Implement reconnection logic
        connection_status["reconnect_count"] += 1
        connection_status["last_connection_attempt"] = datetime.now()

def on_message(client, userdata, msg):
    try:
        # Extract action name from topic
        topic_parts = msg.topic.split('/')
        if len(topic_parts) >= 4 and topic_parts[-1] == "Description":
            action_name = topic_parts[-2]
            try:
                description = msg.payload.decode('utf-8')
            except UnicodeDecodeError as e:
                logger.error(f"Failed to decode message payload: {str(e)}")
                return
            
            # Check if we already have this action
            if action_name in discovered_actions:
                # Only update the description if it changed
                if discovered_actions[action_name] != description:
                    discovered_actions[action_name] = description
                    logger.info(f"Updated action description: {action_name} - {description}")
                return
                
            # New action discovered
            discovered_actions[action_name] = description
            logger.info(f"Discovered new action: {action_name} - {description}")
            
            # Register a dynamic tool for this action if not already registered
            if action_name not in registered_dynamic_tools:
                register_dynamic_action_tool(action_name, description)
    except Exception as e:
        logger.error(f"Error processing MQTT message: {str(e)}", exc_info=True)

def register_dynamic_action_tool(action_name, description):
    try:
        # Skip if already registered
        if action_name in registered_dynamic_tools:
            return
            
        # Create a unique function name for this action
        tool_func_name = f"run_{action_name}"
        
        # Escape any quotes in the description to avoid syntax errors
        escaped_description = description.replace('"', '\\"').replace("'", "\\'")
        
        # We need to create a function with a dynamic name
        # This approach uses exec to create a function with the exact name we want
        exec(f"""
@mcp.tool()
async def {tool_func_name}(ctx: Context) -> str:
    \"\"\"Run the {action_name} action: {escaped_description}\"\"\"
    response = execute_command(f"-runAction {action_name}")
    logger.info(f"Executed action {action_name}")
    return response
""", globals())
        
        # Mark as registered
        registered_dynamic_tools.add(action_name)
        logger.info(f"Registered dynamic tool for action: {action_name} as {tool_func_name}")
    except Exception as e:
        logger.error(f"Failed to register dynamic tool for {action_name}: {str(e)}", exc_info=True)

# Setup MQTT client
def setup_mqtt(args):
    global mqtt_client
    
    # Set logging level from arguments
    try:
        log_level = getattr(logging, args.log_level)
        logger.setLevel(log_level)
        logger.info(f"Log level set to {args.log_level}")
    except AttributeError:
        logger.warning(f"Invalid log level: {args.log_level}, defaulting to INFO")
        logger.setLevel(logging.INFO)
    
    # Use protocol version 5 (MQTT v5) with the newer callback API and unique client ID
    try:
        mqtt_client = mqtt.Client(client_id=args.mqtt_client_id, protocol=mqtt.MQTTv5)
        
        # Set up authentication if provided
        if args.mqtt_user and args.mqtt_password:
            mqtt_client.username_pw_set(args.mqtt_user, args.mqtt_password)
            logger.debug(f"Using MQTT authentication with username: {args.mqtt_user}")
        
        # Configure TLS if enabled
        if args.mqtt_use_tls:
            # Check if certificate files exist before attempting to use them
            cert_files = [
                (args.mqtt_ca_cert, "CA certificate"),
                (args.mqtt_client_cert, "Client certificate"),
                (args.mqtt_client_key, "Client key")
            ]
            
            missing_files = []
            for cert_path, cert_name in cert_files:
                if cert_path and not os.path.exists(cert_path):
                    missing_files.append(f"{cert_name} at {cert_path}")
            
            if missing_files:
                logger.error(f"Missing certificate files: {', '.join(missing_files)}")
                return False
                
            mqtt_client.tls_set(
                ca_certs=args.mqtt_ca_cert,
                certfile=args.mqtt_client_cert,
                keyfile=args.mqtt_client_key
            )
            logger.info("TLS configuration enabled for MQTT connection")
        
        # Set callbacks
        mqtt_client.on_connect = on_connect
        mqtt_client.on_message = on_message
        mqtt_client.on_disconnect = on_disconnect
        
        # Connect to broker
        logger.info(f"Connecting to MQTT broker at {args.mqtt_host}:{args.mqtt_port} with client ID: {args.mqtt_client_id}")
        connection_status["last_connection_attempt"] = datetime.now()
        
        # Set a connection timeout
        try:
            mqtt_client.connect(args.mqtt_host, args.mqtt_port, 60)
            mqtt_client.loop_start()
            
            # Wait briefly to check connection status
            max_wait = 3  # seconds
            for _ in range(max_wait * 2):
                if connection_status["connected"]:
                    logger.info("MQTT client connected successfully")
                    return True
                time.sleep(0.5)
            
            # If we get here, we didn't connect within the timeout
            logger.warning(f"MQTT connection not confirmed after {max_wait} seconds, but loop started")
            return True
            
        except mqtt.MQTTException as e:
            logger.error(f"MQTT protocol error: {str(e)}")
            connection_status["last_error"] = str(e)
            return False
        except ConnectionRefusedError:
            logger.error(f"Connection refused by broker at {args.mqtt_host}:{args.mqtt_port}")
            connection_status["last_error"] = "Connection refused by broker"
            return False
        except TimeoutError:
            logger.error(f"Connection timed out when connecting to {args.mqtt_host}:{args.mqtt_port}")
            connection_status["last_error"] = "Connection timed out"
            return False
            
    except Exception as e:
        logger.error(f"Error setting up MQTT client: {str(e)}", exc_info=True)
        connection_status["last_error"] = str(e)
        return False

# Helper function to execute Coreflux commands
def execute_command(command_string):
    if not mqtt_client:
        error_msg = "MQTT client not initialized"
        logger.error(error_msg)
        return f"ERROR: {error_msg}"
        
    if not connection_status["connected"]:
        error_msg = "MQTT client not connected"
        logger.error(error_msg)
        return f"ERROR: {error_msg}"
    
    # Input validation and sanitization
    command_string = sanitize_command(command_string)
    if not command_string:
        error_msg = "Invalid command after sanitization"
        logger.error(error_msg)
        return f"ERROR: {error_msg}"
        
    try:
        result = mqtt_client.publish("$SYS/Coreflux/Command", command_string)
        if result.rc == mqtt.MQTT_ERR_SUCCESS:
            logger.info(f"Published command: {command_string}")
            return f"Command published successfully: {command_string}"
        else:
            error_msg = f"Failed to publish command: {mqtt.error_string(result.rc)}"
            logger.error(error_msg)
            return f"ERROR: {error_msg}"
    except mqtt.MQTTException as e:
        error_msg = f"MQTT protocol error while executing command: {str(e)}"
        logger.error(error_msg)
        return f"ERROR: {error_msg}"
    except Exception as e:
        error_msg = f"Error executing command: {str(e)}"
        logger.error(error_msg, exc_info=True)
        return f"ERROR: {error_msg}"

# Command sanitization function for basic security
def sanitize_command(command):
    if not isinstance(command, str):
        logger.error("Command must be a string")
        return None
        
    # Remove any potentially dangerous characters or command sequences
    # This is a basic implementation - enhance as needed
    command = command.strip()
    
    # Block any commands that might perform unintended operations
    # This is a simplified example - expand with your specific security requirements
    blocked_patterns = [
        ";", "&&", "||", "`", "$", "(", ")"  # Shell command injection patterns
    ]
    
    for pattern in blocked_patterns:
        if pattern in command:
            logger.warning(f"Potential command injection attempt detected: {command}")
            return None
            
    return command

# Tools for Coreflux commands
@mcp.tool()
@require_auth
async def add_rule(rule_definition: str, ctx: Context) -> str:
    """
    Add a new permission rule to Coreflux
    
    Args:
        rule_definition: The LOT rule definition (DEFINE RULE...)
    """
    if not rule_definition or not rule_definition.strip().startswith("DEFINE RULE"):
        error_msg = "Invalid rule definition format. Must start with 'DEFINE RULE'"
        logger.error(error_msg)
        return f"ERROR: {error_msg}"
        
    logger.info(f"Adding rule: {rule_definition[:50]}..." if len(rule_definition) > 50 else f"Adding rule: {rule_definition}")
    return execute_command(f"-addRule {rule_definition}")

@mcp.tool()
@require_auth
async def remove_rule(rule_name: str, ctx: Context) -> str:
    """Remove a permission rule from Coreflux"""
    if not rule_name or not rule_name.strip():
        error_msg = "Rule name cannot be empty"
        logger.error(error_msg)
        return f"ERROR: {error_msg}"
        
    logger.info(f"Removing rule: {rule_name}")
    return execute_command(f"-removeRule {rule_name}")

@mcp.tool()
@require_auth
async def add_route(ctx: Context) -> str:
    """Add a new route connection"""
    logger.info("Adding new route")
    return execute_command("-addRoute")

@mcp.tool()
@require_auth
async def remove_route(route_id: str, ctx: Context) -> str:
    """Remove a route connection"""
    if not route_id or not route_id.strip():
        error_msg = "Route ID cannot be empty"
        logger.error(error_msg)
        return f"ERROR: {error_msg}"
        
    logger.info(f"Removing route: {route_id}")
    return execute_command(f"-removeRoute {route_id}")

@mcp.tool()
@require_auth
async def add_model(model_definition: str, ctx: Context) -> str:
    """
    Add a new model structure to Coreflux
    
    Args:
        model_definition: The LOT model definition (DEFINE MODEL...)
    """
    if not model_definition or not model_definition.strip().startswith("DEFINE MODEL"):
        error_msg = "Invalid model definition format. Must start with 'DEFINE MODEL'"
        logger.error(error_msg)
        return f"ERROR: {error_msg}"
        
    logger.info(f"Adding model: {model_definition[:50]}..." if len(model_definition) > 50 else f"Adding model: {model_definition}")
    return execute_command(f"-addModel {model_definition}")

@mcp.tool()
@require_auth
async def remove_model(model_name: str, ctx: Context) -> str:
    """Remove a model structure from Coreflux"""
    if not model_name or not model_name.strip():
        error_msg = "Model name cannot be empty"
        logger.error(error_msg)
        return f"ERROR: {error_msg}"
        
    logger.info(f"Removing model: {model_name}")
    return execute_command(f"-removeModel {model_name}")

@mcp.tool()
@require_auth
async def add_action(action_definition: str, ctx: Context) -> str:
    """
    Add a new action event/function to Coreflux
    
    Args:
        action_definition: The LOT action definition (DEFINE ACTION...)
    """
    if not action_definition or not action_definition.strip().startswith("DEFINE ACTION"):
        error_msg = "Invalid action definition format. Must start with 'DEFINE ACTION'"
        logger.error(error_msg)
        return f"ERROR: {error_msg}"
        
    logger.info(f"Adding action: {action_definition[:50]}..." if len(action_definition) > 50 else f"Adding action: {action_definition}")
    return execute_command(f"-addAction {action_definition}")

@mcp.tool()
@require_auth
async def remove_action(action_name: str, ctx: Context) -> str:
    """Remove an action event/function from Coreflux"""
    if not action_name or not action_name.strip():
        error_msg = "Action name cannot be empty"
        logger.error(error_msg)
        return f"ERROR: {error_msg}"
        
    logger.info(f"Removing action: {action_name}")
    return execute_command(f"-removeAction {action_name}")

@mcp.tool()
@require_auth
async def run_action(action_name: str, ctx: Context) -> str:
    """Run an action event/function in Coreflux"""
    if not action_name or not action_name.strip():
        error_msg = "Action name cannot be empty"
        logger.error(error_msg)
        return f"ERROR: {error_msg}"
        
    logger.info(f"Running action: {action_name}")
    return execute_command(f"-runAction {action_name}")

@mcp.tool()
@require_auth
async def remove_all_models(ctx: Context) -> str:
    """Remove all models from Coreflux"""
    logger.warning("Removing ALL models - this is a destructive operation")
    return execute_command("-removeAllModels")

@mcp.tool()
@require_auth
async def remove_all_actions(ctx: Context) -> str:
    """Remove all actions from Coreflux"""
    logger.warning("Removing ALL actions - this is a destructive operation")
    return execute_command("-removeAllActions")

@mcp.tool()
@require_auth
async def remove_all_routes(ctx: Context) -> str:
    """Remove all routes from Coreflux"""
    logger.warning("Removing ALL routes - this is a destructive operation")
    return execute_command("-removeAllRoutes")

@mcp.tool()
@require_auth
async def lot_diagnostic(diagnostic_value: str, ctx: Context) -> str:
    """Change the LOT Diagnostic"""
    if not diagnostic_value or not diagnostic_value.strip():
        error_msg = "Diagnostic value cannot be empty"
        logger.error(error_msg)
        return f"ERROR: {error_msg}"
        
    logger.info(f"Setting LOT diagnostic to: {diagnostic_value}")
    return execute_command(f"-lotDiagnostic {diagnostic_value}")

@mcp.tool()
@require_auth
async def list_discovered_actions(ctx: Context) -> str:
    """List all discovered Coreflux actions"""
    if not discovered_actions:
        logger.info("No actions discovered yet")
        return "No actions discovered yet."
    
    logger.info(f"Listing {len(discovered_actions)} discovered actions")
    result = "Discovered Coreflux Actions:\n\n"
    for action_name, description in discovered_actions.items():
        tool_status = "✓" if action_name in registered_dynamic_tools else "✗"
        result += f"- {action_name}: {description} [Tool: {tool_status}]\n"
    
    return result

@mcp.tool()
@require_auth
async def get_connection_status(ctx: Context) -> str:
    """Get the current MQTT connection status"""
    status = {
        "connected": connection_status["connected"],
        "last_connection_attempt": str(connection_status["last_connection_attempt"]) if connection_status["last_connection_attempt"] else None,
        "reconnect_count": connection_status["reconnect_count"],
        "last_error": connection_status["last_error"],
        "discovered_actions": len(discovered_actions),
        "registered_tools": len(registered_dynamic_tools)
    }
    logger.info(f"Connection status requested: {status}")
    return json.dumps(status, indent=2)

@mcp.tool()
@require_auth
def request_lot_code(ctx: Context, query: str, context: str = "") -> str:
    """
    Request Lot code generation or Lot Knowledge (models, actions, rules) based on a natural language prompt.
    So you are able to create models , actions and rules before adding them. Any 
    logic that you need to implement in the Coreflux MQTT broker you should ask in this tool first. 
    Args:
        query: describe what the user wants in a structured way
        context: Additional context or specific requirements (optional)
    
    Returns:
        str: The reply with documentation and LOT code with the potential actions, models, rules  or routes.
    """
    if not query or not query.strip():
        error_msg = "Query cannot be empty"
        logger.error(error_msg)
        return f"ERROR: {error_msg}"
        
    api_url = "https://anselmo.coreflux.org/webhook/chat_lot_beta"
    
    payload = {
        "query": query,
        "context": context
    }
    
    logger.info(f"Requesting LOT code generation with query: {query[:50]}..." if len(query) > 50 else f"Requesting LOT code generation with query: {query}")
    
    try:
        response = requests.post(api_url, json=payload, timeout=30)
        if response.status_code == 200:
            try:
                # Get the JSON response
                result = response.json()
                
                # For formatted output in the log
                formatted_json = json.dumps(result, indent=2)
                logger.info(f"LOT code generation successful: {formatted_json[:200]}...")
                
                # Return the result directly as a string, with proper formatting
                # Use a structured format for better readability
                output = []
                
                if "title" in result:
                    output.append(f"# {result['title']}")
                    output.append("")
                
                if "description" in result:
                    output.append(result['description'])
                    output.append("")
                
                if "lot_code" in result:
                    output.append("```")
                    output.append(result['lot_code'])
                    output.append("```")
                    output.append("")
                
                if "explanation" in result:
                    output.append("## Explanation")
                    output.append(result['explanation'])
                
                # Join all parts with newlines and return
                return "\n".join(output)
                
            except json.JSONDecodeError as e:
                error_msg = f"Failed to parse API response: {str(e)}"
                logger.error(error_msg)
                return f"Error: {error_msg}"
        else:
            error_msg = f"API request failed with status {response.status_code}"
            logger.error(error_msg)
            return f"Error: {error_msg}"
    except requests.exceptions.Timeout:
        error_msg = "API request timed out after 30 seconds"
        logger.error(error_msg)
        return f"Error: {error_msg}"
    except requests.exceptions.ConnectionError:
        error_msg = "Connection error occurred when making API request"
        logger.error(error_msg)
        return f"Error: {error_msg}"
    except Exception as e:
        error_msg = f"Error making API request: {str(e)}"
        logger.error(error_msg, exc_info=True)
        return f"Error: {error_msg}"

# Resources for LOT language documentation
@mcp.resource("lot://documentation/models")
def lot_models_docs() -> str:
    """Documentation for LOT Models"""
    return """
# LOT Language - Model Management Documentation

## 1. Overview
Models in Coreflux use the LOT language syntax to define how data is processed, transformed, and published. Models take input data (triggered by specific topics), process it through expressions, constants, or transformations, and output the results to new MQTT topics.

## 2. Model Syntax
```
DEFINE MODEL <model_name> WITH TOPIC "<output_base_topic>"
    ADD "<property_name>" WITH TOPIC "<input_topic>" [AS TRIGGER]
    ADD "<property_name>" WITH <constant_value>
    ADD "<property_name>" WITH (expression)
```

## 3. Example Model
```
DEFINE MODEL GenericEnergyCost WITH TOPIC "Coreflux/+/+/+/+/energy"
    ADD "total_energy" WITH TOPIC "shellies/+/+/+/+/device/energy" AS TRIGGER
    ADD "energy_price" WITH 3
    ADD "cost" WITH (total_energy * energy_price)
```
"""

@mcp.resource("lot://documentation/rules")
def lot_rules_docs() -> str:
    """Documentation for LOT Rules"""
    return """
# LOT Language - Rule Management Documentation

## 1. Overview
Rules in Coreflux govern user permissions and system actions, ensuring precise control over system operations.

## 2. Rule Syntax
```
DEFINE RULE <rule_name> WITH PRIORITY <priority_value> FOR <action_scope>
    IF <condition> THEN
        ALLOW
    ELSE
        DENY
```

## 3. Example Rule
```
DEFINE RULE SpecificTopicClient WITH PRIORITY 1 FOR Subscribe TO TOPIC "Emanuel/#"
    IF USER IS "Emanuel" THEN
        ALLOW
    ELSE
        DENY
```
"""

@mcp.resource("lot://documentation/actions")
def lot_actions_docs() -> str:
    """Documentation for LOT Actions"""
    return """
# LOT Language - Action Management Documentation

## 1. Overview
LOT scripting language defines Actions—small logic blocks that react to events (time-based or topic-based) and publish data to topics.

## 2. Action Syntax
```
DEFINE ACTION <ActionName>
ON EVERY ... or ON TOPIC ... or only DO 
DO
    IF <expression> THEN
        PUBLISH ...
    ELSE
        PUBLISH ...
```


## 3. Example Action that runs every 5 seconds 
```
DEFINE ACTION StrokeGenerator
ON EVERY 5 SECONDS 
DO
    IF GET TOPIC "Coreflux/Porto/MeetingRoom/Light1/command/switch:0" == "off" THEN
        PUBLISH TOPIC "Coreflux/Porto/MeetingRoom/Light1/command/switch:0" WITH "on"
    ELSE
        PUBLISH TOPIC "Coreflux/Porto/MeetingRoom/Light1/command/switch:0" WITH "off"
```
## 4. Example Action that can be called by run action 
```
DEFINE ACTION TurnLampOff
DO
    PUBLISH TOPIC "Coreflux/Porto/MeetingRoom/Light1/command/switch:0" WITH "off"
DESCRIPTION "Turns a specific topic off"
```



"""

@mcp.tool()
@require_auth
async def reconnect_mqtt(ctx: Context) -> str:
    """Force a reconnection to the MQTT broker"""
    if mqtt_client is None:
        error_msg = "Cannot reconnect - MQTT client not initialized"
        logger.error(error_msg)
        return f"ERROR: {error_msg}"
        
    try:
        # First disconnect if connected
        if connection_status["connected"]:
            mqtt_client.disconnect()
            
        # Parse args again to get current settings
        args = parse_args()
        
        # Connect with current settings
        logger.info(f"Attempting to reconnect to MQTT broker at {args.mqtt_host}:{args.mqtt_port}")
        connection_status["last_connection_attempt"] = datetime.now()
        mqtt_client.connect(args.mqtt_host, args.mqtt_port, 60)
        
        # Give it a moment to connect
        time.sleep(1)
        
        if connection_status["connected"]:
            logger.info("Reconnection successful")
            return "Successfully reconnected to MQTT broker"
        else:
            logger.warning("Reconnection attempt completed but connection not confirmed")
            return "Reconnection attempt completed but connection not confirmed. Check logs for details."
    except Exception as e:
        error_msg = f"Failed to reconnect: {str(e)}"
        logger.error(error_msg, exc_info=True)
        return f"ERROR: {error_msg}"

@mcp.tool()
@require_auth
async def check_broker_health(ctx: Context) -> str:
    """Check the health of the MQTT broker and attempt to reconnect if needed"""
    if not mqtt_client:
        error_msg = "MQTT client not initialized"
        logger.error(error_msg)
        return f"ERROR: {error_msg}"
        
    if connection_status["connected"]:
        logger.info("MQTT broker connection is healthy")
        return "MQTT broker connection is healthy"
    else:
        logger.warning("MQTT broker connection appears to be down, attempting to reconnect")
        return await reconnect_mqtt(ctx)

def run_setup_assistant():
    """
    Interactive setup assistant for first-time configuration.
    Creates or updates the .env file with user-provided values.
    """
    print("\n" + "="*50)
    print("Coreflux MCP Server - Setup Assistant")
    print("="*50)
    print("This assistant will help you configure the server by creating or updating the .env file.")
    print("Press Enter to accept the default value shown in brackets [default].")
    print("-"*50)
    
    # Check if .env file exists
    env_file_exists = os.path.isfile(".env")
    env_vars = {}
    
    if env_file_exists:
        print("Existing .env file found. Current values will be shown as defaults.")
        # Read existing values
        with open(".env", "r") as f:
            for line in f:
                if line.strip() and not line.strip().startswith("#"):
                    try:
                        key, value = line.strip().split("=", 1)
                        env_vars[key] = value
                    except ValueError:
                        # Skip lines that don't have a key=value format
                        pass
    
    # MQTT Broker Configuration
    mqtt_broker = input(f"MQTT Broker Host [{ env_vars.get('MQTT_BROKER', 'localhost') }]: ").strip()
    env_vars["MQTT_BROKER"] = mqtt_broker if mqtt_broker else env_vars.get("MQTT_BROKER", "localhost")
    
    mqtt_port = input(f"MQTT Broker Port [{ env_vars.get('MQTT_PORT', '1883') }]: ").strip()
    env_vars["MQTT_PORT"] = mqtt_port if mqtt_port else env_vars.get("MQTT_PORT", "1883")
    
    mqtt_user = input(f"MQTT Username [{ env_vars.get('MQTT_USER', 'root') }]: ").strip()
    env_vars["MQTT_USER"] = mqtt_user if mqtt_user else env_vars.get("MQTT_USER", "root")
    
    mqtt_password = input(f"MQTT Password [{ env_vars.get('MQTT_PASSWORD', 'coreflux') }]: ").strip()
    env_vars["MQTT_PASSWORD"] = mqtt_password if mqtt_password else env_vars.get("MQTT_PASSWORD", "coreflux")
    
    mqtt_client_id = input(f"MQTT Client ID [{ env_vars.get('MQTT_CLIENT_ID', f'coreflux-mcp-{uuid.uuid4().hex[:8]}') }]: ").strip()
    env_vars["MQTT_CLIENT_ID"] = mqtt_client_id if mqtt_client_id else env_vars.get("MQTT_CLIENT_ID", f"coreflux-mcp-{uuid.uuid4().hex[:8]}")
    
    # TLS Configuration
    use_tls = input(f"Use TLS for MQTT connection (true/false) [{ env_vars.get('MQTT_USE_TLS', 'false') }]: ").strip().lower()
    if use_tls in ["true", "false"]:
        env_vars["MQTT_USE_TLS"] = use_tls
    else:
        env_vars["MQTT_USE_TLS"] = env_vars.get("MQTT_USE_TLS", "false")
    
    if env_vars["MQTT_USE_TLS"] == "true":
        ca_cert = input(f"Path to CA Certificate [{ env_vars.get('MQTT_CA_CERT', '') }]: ").strip()
        env_vars["MQTT_CA_CERT"] = ca_cert if ca_cert else env_vars.get("MQTT_CA_CERT", "")
        
        client_cert = input(f"Path to Client Certificate [{ env_vars.get('MQTT_CLIENT_CERT', '') }]: ").strip()
        env_vars["MQTT_CLIENT_CERT"] = client_cert if client_cert else env_vars.get("MQTT_CLIENT_CERT", "")
        
        client_key = input(f"Path to Client Key [{ env_vars.get('MQTT_CLIENT_KEY', '') }]: ").strip()
        env_vars["MQTT_CLIENT_KEY"] = client_key if client_key else env_vars.get("MQTT_CLIENT_KEY", "")
    
    # Logging Configuration
    log_level = input(f"Log Level (DEBUG/INFO/WARNING/ERROR/CRITICAL) [{ env_vars.get('LOG_LEVEL', 'INFO') }]: ").strip().upper()
    valid_log_levels = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
    if log_level in valid_log_levels:
        env_vars["LOG_LEVEL"] = log_level
    else:
        env_vars["LOG_LEVEL"] = env_vars.get("LOG_LEVEL", "INFO")
    
    # Write to .env file
    with open(".env", "w") as f:
        f.write("# Coreflux MCP Server Configuration\n")
        f.write("# Generated by Setup Assistant\n")
        f.write(f"# {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")
        
        # MQTT Configuration
        f.write("# MQTT Broker Configuration\n")
        f.write(f"MQTT_BROKER={env_vars['MQTT_BROKER']}\n")
        f.write(f"MQTT_PORT={env_vars['MQTT_PORT']}\n")
        f.write(f"MQTT_USER={env_vars['MQTT_USER']}\n")
        f.write(f"MQTT_PASSWORD={env_vars['MQTT_PASSWORD']}\n")
        f.write(f"MQTT_CLIENT_ID={env_vars['MQTT_CLIENT_ID']}\n\n")
        
        # TLS Configuration
        f.write("# TLS Configuration\n")
        f.write(f"MQTT_USE_TLS={env_vars['MQTT_USE_TLS']}\n")
        if env_vars["MQTT_USE_TLS"] == "true":
            f.write(f"MQTT_CA_CERT={env_vars['MQTT_CA_CERT']}\n")
            f.write(f"MQTT_CLIENT_CERT={env_vars['MQTT_CLIENT_CERT']}\n")
            f.write(f"MQTT_CLIENT_KEY={env_vars['MQTT_CLIENT_KEY']}\n\n")
        
        # Logging Configuration
        f.write("# Logging Configuration\n")
        f.write(f"LOG_LEVEL={env_vars['LOG_LEVEL']}\n")
    
    print("\nConfiguration saved to .env file successfully!")
    print("-"*50)
    print("You can disable this setup assistant by setting ENABLE_SETUP_ASSISTANT = False in the server.py file.")
    print("="*50 + "\n")
    
    # Reload environment variables
    load_dotenv(override=True)

# Initialize authentication system
def init_auth_system():
    """Initialize the authentication system"""
    auth_file_path = "auth_config.json"
    
    # Check if auth file exists
    if os.path.exists(auth_file_path):
        try:
            with open(auth_file_path, "r") as f:
                auth_data = json.load(f)
                
            # Update global auth data
            user_auth["users"] = auth_data.get("users", {})
            user_auth["permissions"] = auth_data.get("permissions", DEFAULT_PERMISSIONS)
            
            logger.info(f"Loaded {len(user_auth['users'])} users from auth config")
        except Exception as e:
            logger.error(f"Failed to load auth configuration: {str(e)}")
            # Use defaults
            create_default_admin()
    else:
        # Create default admin user
        create_default_admin()
        # Save to file
        save_auth_config()
        
    logger.info("Authentication system initialized")

# Create default admin user
def create_default_admin():
    """Create a default admin user if none exists"""
    if not user_auth["users"]:
        # Create default admin
        password_hash = hash_password(DEFAULT_ADMIN_PASSWORD)
        user_auth["users"]["admin"] = {
            "password_hash": password_hash,
            "role": "admin",
            "created_at": datetime.now().isoformat()
        }
        logger.warning("Created default admin user with default password. Please change it!")

# Save auth configuration to file
def save_auth_config():
    """Save the current auth configuration to file"""
    auth_file_path = "auth_config.json"
    
    # Don't save tokens to the file (they are ephemeral)
    save_data = {
        "users": user_auth["users"],
        "permissions": user_auth["permissions"]
    }
    
    try:
        with open(auth_file_path, "w") as f:
            json.dump(save_data, f, indent=2)
        logger.info("Auth configuration saved to disk")
        return True
    except Exception as e:
        logger.error(f"Failed to save auth configuration: {str(e)}")
        return False

# Password hashing
def hash_password(password: str) -> str:
    """Hash a password for storage"""
    if not password:
        raise ValueError("Password cannot be empty")
        
    salt = secrets.token_hex(16)
    hash_obj = hashlib.sha256((password + salt).encode())
    password_hash = hash_obj.hexdigest()
    
    return f"{salt}:{password_hash}"

# Password verification
def verify_password(stored_hash: str, provided_password: str) -> bool:
    """Verify a password against a stored hash"""
    if not stored_hash or not provided_password:
        return False
        
    try:
        salt, hash_value = stored_hash.split(":", 1)
        hash_obj = hashlib.sha256((provided_password + salt).encode())
        return hash_obj.hexdigest() == hash_value
    except Exception:
        return False

# Token generation
def generate_auth_token(username: str) -> str:
    """Generate an authentication token for a user"""
    if username not in user_auth["users"]:
        raise ValueError(f"User {username} does not exist")
        
    token = secrets.token_urlsafe(32)
    expiry = time.time() + AUTH_TOKEN_EXPIRY
    
    user_auth["tokens"][token] = {
        "username": username,
        "expiry": expiry,
        "created_at": time.time()
    }
    
    return token

# Token validation
def validate_token(token: str) -> Optional[str]:
    """Validate a token and return the username if valid"""
    if token not in user_auth["tokens"]:
        return None
        
    token_data = user_auth["tokens"][token]
    
    # Check if token has expired
    if token_data["expiry"] < time.time():
        # Remove expired token
        del user_auth["tokens"][token]
        return None
        
    return token_data["username"]

# Check user permissions
def user_has_permission(username: str, permission: str) -> bool:
    """Check if a user has a specific permission"""
    if username not in user_auth["users"]:
        return False
        
    role = user_auth["users"][username]["role"]
    
    # Get permissions for role
    role_permissions = user_auth["permissions"].get(role, [])
    
    # Admin role has all permissions
    if "*" in role_permissions:
        return True
        
    return permission in role_permissions

# Authentication decorator for MCP tools
def require_auth(func):
    """Decorator to require authentication for MCP tools"""
    async def wrapper(ctx: Context, *args, **kwargs):
        # Extract token from context
        headers = getattr(ctx, "headers", {})
        token = headers.get("Authorization", "").replace("Bearer ", "")
        
        username = validate_token(token)
        if not username:
            return "ERROR: Authentication required or token expired"
            
        # Check permission for this function
        permission = func.__name__
        if not user_has_permission(username, permission):
            logger.warning(f"User {username} attempted to access {permission} without permission")
            return f"ERROR: You don't have permission to use {permission}"
            
        # Add username to context for the function to use
        ctx.username = username
        
        # Call the original function
        return await func(ctx, *args, **kwargs)
        
    # Preserve function metadata
    wrapper.__name__ = func.__name__
    wrapper.__doc__ = func.__doc__
    
    return wrapper

@mcp.tool()
@require_auth
async def manage_user(ctx: Context, action: str, username: str, password: str = None, role: str = None) -> str:
    """
    Manage users (create, update, delete)
    
    Args:
        action: The action to perform (create, update, delete)
        username: Username to manage
        password: Password for create/update operations (optional for update)
        role: User role (admin, user, readonly) for create/update operations
    """
    # Check if user has admin role
    if not hasattr(ctx, "username") or ctx.username != "admin":
        error_msg = "Only admin users can manage users"
        logger.error(error_msg)
        return f"ERROR: {error_msg}"
        
    action = action.lower()
    
    if action == "create":
        # Validate inputs
        if not username or not password or not role:
            error_msg = "Username, password, and role are required for user creation"
            logger.error(error_msg)
            return f"ERROR: {error_msg}"
            
        # Check if role is valid
        if role not in USER_ROLES:
            error_msg = f"Invalid role. Allowed roles: {', '.join(USER_ROLES)}"
            logger.error(error_msg)
            return f"ERROR: {error_msg}"
            
        # Check if user already exists
        if username in user_auth["users"]:
            error_msg = f"User {username} already exists"
            logger.error(error_msg)
            return f"ERROR: {error_msg}"
            
        # Create user
        password_hash = hash_password(password)
        user_auth["users"][username] = {
            "password_hash": password_hash,
            "role": role,
            "created_at": datetime.now().isoformat()
        }
        
        # Save configuration
        if save_auth_config():
            logger.info(f"Created user {username} with role {role}")
            return f"User {username} created successfully with role {role}"
        else:
            error_msg = "Failed to save auth configuration"
            logger.error(error_msg)
            return f"ERROR: {error_msg}"
            
    elif action == "update":
        # Check if user exists
        if username not in user_auth["users"]:
            error_msg = f"User {username} does not exist"
            logger.error(error_msg)
            return f"ERROR: {error_msg}"
            
        # Update password if provided
        if password:
            password_hash = hash_password(password)
            user_auth["users"][username]["password_hash"] = password_hash
            logger.info(f"Updated password for user {username}")
            
        # Update role if provided
        if role:
            if role not in USER_ROLES:
                error_msg = f"Invalid role. Allowed roles: {', '.join(USER_ROLES)}"
                logger.error(error_msg)
                return f"ERROR: {error_msg}"
                
            user_auth["users"][username]["role"] = role
            logger.info(f"Updated role for user {username} to {role}")
            
        # Save configuration
        if save_auth_config():
            return f"User {username} updated successfully"
        else:
            error_msg = "Failed to save auth configuration"
            logger.error(error_msg)
            return f"ERROR: {error_msg}"
            
    elif action == "delete":
        # Check if user exists
        if username not in user_auth["users"]:
            error_msg = f"User {username} does not exist"
            logger.error(error_msg)
            return f"ERROR: {error_msg}"
            
        # Prevent admin from deleting themselves
        if username == "admin":
            error_msg = "Cannot delete the admin user"
            logger.error(error_msg)
            return f"ERROR: {error_msg}"
            
        # Delete user
        del user_auth["users"][username]
        
        # Also remove any active tokens for this user
        for token, token_data in list(user_auth["tokens"].items()):
            if token_data["username"] == username:
                del user_auth["tokens"][token]
                
        # Save configuration
        if save_auth_config():
            logger.info(f"Deleted user {username}")
            return f"User {username} deleted successfully"
        else:
            error_msg = "Failed to save auth configuration"
            logger.error(error_msg)
            return f"ERROR: {error_msg}"
    else:
        error_msg = f"Invalid action. Allowed actions: create, update, delete"
        logger.error(error_msg)
        return f"ERROR: {error_msg}"

@mcp.tool()
async def login(ctx: Context, username: str, password: str) -> str:
    """
    Login and get an authentication token
    
    Args:
        username: Username
        password: Password
    """
    # Check if user exists
    if username not in user_auth["users"]:
        error_msg = f"Invalid username or password"
        logger.error(f"Login attempt with invalid username: {username}")
        return f"ERROR: {error_msg}"
        
    # Verify password
    stored_hash = user_auth["users"][username]["password_hash"]
    if not verify_password(stored_hash, password):
        error_msg = f"Invalid username or password"
        logger.error(f"Login attempt with invalid password for user: {username}")
        return f"ERROR: {error_msg}"
        
    # Generate token
    try:
        token = generate_auth_token(username)
        logger.info(f"User {username} logged in successfully")
        
        # Return token and role
        role = user_auth["users"][username]["role"]
        return json.dumps({
            "token": token,
            "username": username,
            "role": role,
            "expires_in": AUTH_TOKEN_EXPIRY
        })
    except Exception as e:
        error_msg = f"Error generating token: {str(e)}"
        logger.error(error_msg)
        return f"ERROR: {error_msg}"

@mcp.tool()
@require_auth
async def logout(ctx: Context) -> str:
    """Logout and invalidate the current authentication token"""
    headers = getattr(ctx, "headers", {})
    token = headers.get("Authorization", "").replace("Bearer ", "")
    
    if token in user_auth["tokens"]:
        username = user_auth["tokens"][token]["username"]
        del user_auth["tokens"][token]
        logger.info(f"User {username} logged out")
        return "Logged out successfully"
    else:
        return "No active session to logout"

@mcp.tool()
@require_auth
async def list_users(ctx: Context) -> str:
    """List all users (admin only)"""
    # Check if user has admin role
    if not hasattr(ctx, "username") or ctx.username != "admin":
        error_msg = "Only admin users can list users"
        logger.error(error_msg)
        return f"ERROR: {error_msg}"
        
    # Get users and roles
    users_list = []
    for username, user_data in user_auth["users"].items():
        users_list.append({
            "username": username,
            "role": user_data["role"],
            "created_at": user_data.get("created_at", "Unknown")
        })
        
    return json.dumps(users_list, indent=2)

# Function to check for updates
def check_for_updates():
    """Check GitHub for the latest version of the server"""
    global UPDATE_AVAILABLE, LATEST_VERSION
    
    try:
        logger.info("Checking for updates...")
        response = requests.get(GITHUB_API_URL, timeout=10)
        
        if response.status_code == 200:
            release_info = response.json()
            latest_version = release_info.get("tag_name", "").lstrip("v")
            
            # Compare versions
            if latest_version and compare_versions(latest_version, SERVER_VERSION) > 0:
                UPDATE_AVAILABLE = True
                LATEST_VERSION = latest_version
                logger.info(f"Update available: v{latest_version} (current: v{SERVER_VERSION})")
            else:
                logger.info(f"Server is up to date (v{SERVER_VERSION})")
        else:
            logger.warning(f"Failed to check for updates: HTTP {response.status_code}")
    except Exception as e:
        logger.error(f"Error checking for updates: {str(e)}")
    
    # Schedule next check
    threading.Timer(VERSION_CHECK_INTERVAL, check_for_updates).start()

# Function to compare semantic versions
def compare_versions(version1, version2):
    """
    Compare two semantic versions.
    Returns:
    - Positive number if version1 > version2
    - 0 if version1 == version2
    - Negative number if version1 < version2
    """
    v1_parts = [int(x) for x in version1.split(".")]
    v2_parts = [int(x) for x in version2.split(".")]
    
    # Pad with zeros if necessary
    while len(v1_parts) < 3:
        v1_parts.append(0)
    while len(v2_parts) < 3:
        v2_parts.append(0)
    
    # Compare major, minor, patch
    for i in range(3):
        if v1_parts[i] != v2_parts[i]:
            return v1_parts[i] - v2_parts[i]
    
    return 0

# Check for known vulnerabilities in current version
def check_vulnerabilities():
    """Check if current version has known vulnerabilities"""
    vulnerable_versions = []
    for version, description in VULNERABILITIES.items():
        if compare_versions(version, SERVER_VERSION) >= 0:
            # Current version is same or older than a version with known vulnerability
            vulnerable_versions.append((version, description))
    
    if vulnerable_versions:
        for version, description in vulnerable_versions:
            logger.critical(f"SECURITY VULNERABILITY: Version {version} - {description}")
        logger.critical(f"Current version {SERVER_VERSION} is vulnerable. Update immediately!")
        return True
    
    return False

@mcp.tool()
@require_auth
async def check_update(ctx: Context) -> str:
    """Check if a new version of the server is available"""
    global UPDATE_AVAILABLE, LATEST_VERSION
    
    try:
        # Force a check now
        response = requests.get(GITHUB_API_URL, timeout=10)
        
        if response.status_code == 200:
            release_info = response.json()
            latest_version = release_info.get("tag_name", "").lstrip("v")
            
            # Compare versions
            if latest_version and compare_versions(latest_version, SERVER_VERSION) > 0:
                UPDATE_AVAILABLE = True
                LATEST_VERSION = latest_version
                
                # Get release notes
                release_notes = release_info.get("body", "No release notes available")
                
                result = {
                    "current_version": SERVER_VERSION,
                    "latest_version": latest_version,
                    "update_available": True,
                    "update_url": GITHUB_REPO_URL + "/releases/latest",
                    "release_notes": release_notes
                }
                
                logger.info(f"Update available: v{latest_version} (current: v{SERVER_VERSION})")
                return json.dumps(result, indent=2)
            else:
                result = {
                    "current_version": SERVER_VERSION,
                    "update_available": False,
                    "message": "Server is up to date."
                }
                logger.info(f"Server is up to date (v{SERVER_VERSION})")
                return json.dumps(result, indent=2)
        else:
            error_msg = f"Failed to check for updates: HTTP {response.status_code}"
            logger.warning(error_msg)
            return f"ERROR: {error_msg}"
    except Exception as e:
        error_msg = f"Error checking for updates: {str(e)}"
        logger.error(error_msg)
        return f"ERROR: {error_msg}"

@mcp.tool()
@require_auth
async def get_server_info(ctx: Context) -> str:
    """Get information about the server, including version and uptime"""
    uptime = datetime.now() - server_start_time
    uptime_str = str(uptime).split('.')[0]  # Remove microseconds
    
    # Check for updates if we haven't already
    if UPDATE_AVAILABLE is None:
        try:
            check_for_updates()
        except Exception as e:
            logger.error(f"Error checking for updates: {str(e)}")
    
    # Check for vulnerabilities
    vulnerabilities = check_vulnerabilities()
    
    info = {
        "server_version": SERVER_VERSION,
        "uptime": uptime_str,
        "start_time": server_start_time.isoformat(),
        "python_version": sys.version,
        "update_available": UPDATE_AVAILABLE,
        "security_vulnerabilities": vulnerabilities,
        "mqtt_status": {
            "connected": connection_status["connected"],
            "reconnect_count": connection_status["reconnect_count"]
        }
    }
    
    if UPDATE_AVAILABLE and LATEST_VERSION:
        info["latest_version"] = LATEST_VERSION
        info["update_url"] = GITHUB_REPO_URL + "/releases/latest"
    
    logger.info("Server info requested")
    return json.dumps(info, indent=2)

# Enhanced logging with additional context
def enhanced_logger(func):
    @wraps(func)
    async def wrapper(*args, **kwargs):
        # Get function details
        fn_name = func.__name__
        fn_doc = func.__doc__.split("\n")[0] if func.__doc__ else "No description"
        
        # Log the function call with parameters (excluding ctx)
        param_str = ", ".join([f"{k}={repr(v)}" for k, v in kwargs.items() if k != "ctx"])
        logger.debug(f"CALL: {fn_name}({param_str})")
        
        start_time = time.time()
        try:
            # Call the original function
            result = await func(*args, **kwargs)
            
            # Calculate execution time
            exec_time = (time.time() - start_time) * 1000  # milliseconds
            
            # Log successful execution
            logger.debug(f"SUCCESS: {fn_name} completed in {exec_time:.2f}ms")
            
            return result
        except Exception as e:
            # Calculate execution time until error
            exec_time = (time.time() - start_time) * 1000  # milliseconds
            
            # Get current stack trace for debugging
            stack_trace = traceback.format_exc()
            
            # Log the error with details
            logger.error(f"ERROR in {fn_name} after {exec_time:.2f}ms: {str(e)}\n{stack_trace}")
            
            # Re-raise the exception
            raise
            
    return wrapper

# System health monitoring functions
def get_system_health():
    """Get system health metrics including CPU, memory, disk, and network"""
    try:
        # System info
        system_info = {
            "platform": platform.platform(),
            "python_version": platform.python_version(),
            "hostname": socket.gethostname()
        }
        
        # CPU info
        cpu_percent = psutil.cpu_percent(interval=0.1)
        cpu_count = psutil.cpu_count()
        
        # Memory info
        memory = psutil.virtual_memory()
        memory_info = {
            "total": memory.total,
            "available": memory.available,
            "percent": memory.percent,
            "used": memory.used,
            "free": memory.free
        }
        
        # Disk info
        disk = psutil.disk_usage('/')
        disk_info = {
            "total": disk.total,
            "used": disk.used,
            "free": disk.free,
            "percent": disk.percent
        }
        
        # Network info
        network_info = {}
        network_stats = psutil.net_io_counters(pernic=True)
        for interface, stats in network_stats.items():
            network_info[interface] = {
                "bytes_sent": stats.bytes_sent,
                "bytes_recv": stats.bytes_recv,
                "packets_sent": stats.packets_sent,
                "packets_recv": stats.packets_recv,
                "errin": stats.errin,
                "errout": stats.errout,
                "dropin": stats.dropin,
                "dropout": stats.dropout
            }
        
        # Process info
        process = psutil.Process()
        process_info = {
            "cpu_percent": process.cpu_percent(interval=0.1),
            "memory_percent": process.memory_percent(),
            "memory_info": {
                "rss": process.memory_info().rss,
                "vms": process.memory_info().vms
            },
            "threads": len(process.threads()),
            "connections": len(process.connections())
        }
        
        return {
            "timestamp": datetime.now().isoformat(),
            "system": system_info,
            "cpu": {
                "percent": cpu_percent,
                "count": cpu_count
            },
            "memory": memory_info,
            "disk": disk_info,
            "network": network_info,
            "process": process_info
        }
    except Exception as e:
        logger.error(f"Error getting system health: {str(e)}")
        return {"error": str(e)}

@mcp.tool()
@require_auth
async def health_check(ctx: Context) -> str:
    """
    Perform a comprehensive health check of the MCP server and its connections
    
    Returns detailed health status information for monitoring and diagnostics
    """
    try:
        # Get system health
        system_health = get_system_health()
        
        # MQTT connection health
        mqtt_health = {
            "connected": connection_status["connected"],
            "reconnect_count": connection_status["reconnect_count"],
            "last_error": connection_status["last_error"],
            "last_connection_attempt": str(connection_status["last_connection_attempt"]) if connection_status["last_connection_attempt"] else None
        }
        
        # Server stats
        server_uptime = datetime.now() - server_start_time
        
        # Format uptime nicely
        uptime_days = server_uptime.days
        uptime_hours, remainder = divmod(server_uptime.seconds, 3600)
        uptime_minutes, uptime_seconds = divmod(remainder, 60)
        
        formatted_uptime = ""
        if uptime_days > 0:
            formatted_uptime += f"{uptime_days}d "
        formatted_uptime += f"{uptime_hours}h {uptime_minutes}m {uptime_seconds}s"
        
        server_stats = {
            "version": SERVER_VERSION,
            "uptime": formatted_uptime,
            "start_time": server_start_time.isoformat(),
            "discovered_actions": len(discovered_actions),
            "registered_tools": len(registered_dynamic_tools),
            "active_users": len(user_auth["tokens"])
        }
        
        # Disk space for logs
        log_directory = os.path.dirname(os.path.abspath("coreflux_mcp.log"))
        disk_stats = psutil.disk_usage(log_directory)
        log_stats = {
            "log_directory": log_directory,
            "disk_space": {
                "total": disk_stats.total,
                "used": disk_stats.used,
                "free": disk_stats.free,
                "percent": disk_stats.percent
            }
        }
        
        # Check log file size
        try:
            log_file_size = os.path.getsize("coreflux_mcp.log")
            log_stats["log_file_size"] = log_file_size
        except Exception as e:
            log_stats["log_file_size"] = f"Error: {str(e)}"
        
        # Check updates
        update_status = {
            "current_version": SERVER_VERSION,
            "update_available": UPDATE_AVAILABLE,
            "latest_version": LATEST_VERSION
        }
        
        # Combine all health data
        health_data = {
            "timestamp": datetime.now().isoformat(),
            "status": "healthy" if connection_status["connected"] else "unhealthy",
            "system": system_health,
            "mqtt": mqtt_health,
            "server": server_stats,
            "logs": log_stats,
            "updates": update_status
        }
        
        # Return as formatted JSON
        logger.info("Health check performed")
        return json.dumps(health_data, indent=2)
    except Exception as e:
        error_msg = f"Error performing health check: {str(e)}"
        logger.error(error_msg, exc_info=True)
        return f"ERROR: {error_msg}"

@mcp.tool()
@require_auth
async def rotate_logs(ctx: Context) -> str:
    """
    Rotate the server logs to prevent them from growing too large
    
    This will rename the current log file and start a new one
    """
    try:
        # Check if user has admin role
        if not hasattr(ctx, "username") or not user_has_permission(ctx.username, "rotate_logs"):
            error_msg = "You don't have permission to rotate logs"
            logger.error(error_msg)
            return f"ERROR: {error_msg}"
            
        log_file = "coreflux_mcp.log"
        
        # Check if log file exists
        if not os.path.exists(log_file):
            return "No log file exists to rotate"
            
        # Get current timestamp for the backup filename
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup_log = f"coreflux_mcp-{timestamp}.log"
        
        # Close current log handlers
        for handler in logger.handlers[:]:
            if isinstance(handler, logging.FileHandler):
                handler.close()
                logger.removeHandler(handler)
        
        # Rename the file
        os.rename(log_file, backup_log)
        
        # Add new file handler
        file_handler = logging.FileHandler(log_file)
        file_handler.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levellevel)s - %(message)s'))
        logger.addHandler(file_handler)
        
        # Log the rotation
        logger.info(f"Log file rotated to {backup_log}")
        
        return f"Log file rotated successfully. Previous log saved as {backup_log}"
    except Exception as e:
        error_msg = f"Error rotating logs: {str(e)}"
        logger.error(error_msg, exc_info=True)
        return f"ERROR: {error_msg}"

@mcp.tool()
@require_auth
async def troubleshoot(ctx: Context, issue_type: str = None) -> str:
    """
    Run troubleshooting diagnostics to identify common issues
    
    Args:
        issue_type: Optional - The type of issue to troubleshoot (mqtt, auth, performance, all)
    """
    issue_type = issue_type.lower() if issue_type else "all"
    valid_types = ["mqtt", "auth", "performance", "all"]
    
    if issue_type not in valid_types:
        return f"Invalid issue type. Please specify one of: {', '.join(valid_types)}"
    
    results = []
    
    # MQTT connection issues
    if issue_type in ["mqtt", "all"]:
        results.append("## MQTT Troubleshooting")
        
        # Check connection status
        if connection_status["connected"]:
            results.append("✅ MQTT connection: Connected")
        else:
            results.append("❌ MQTT connection: Disconnected")
            results.append(f"   Last error: {connection_status['last_error']}")
            results.append(f"   Reconnection attempts: {connection_status['reconnect_count']}")
            
            # Suggest fixes
            results.append("\nPossible solutions:")
            results.append("1. Check if the MQTT broker is running")
            results.append("2. Verify MQTT credentials are correct")
            results.append("3. Check network connectivity to the broker")
            results.append("4. Run `reconnect_mqtt` tool to attempt reconnection")
        
        # Check topic subscriptions
        if connection_status["connected"]:
            results.append("\nDiscovered actions: " + (str(len(discovered_actions)) if discovered_actions else "None"))
            if not discovered_actions:
                results.append("⚠️ No actions discovered. Possible causes:")
                results.append("1. No actions defined in the Coreflux broker")
                results.append("2. Subscription to action descriptions not working")
                results.append("3. Broker not publishing action descriptions")
        
        results.append("")
    
    # Authentication issues
    if issue_type in ["auth", "all"]:
        results.append("## Authentication Troubleshooting")
        
        # Check if auth system is initialized
        if user_auth["users"]:
            results.append(f"✅ Authentication system: Initialized with {len(user_auth['users'])} users")
            
            # Check default password
            default_admin = False
            if "admin" in user_auth["users"]:
                stored_hash = user_auth["users"]["admin"]["password_hash"]
                if verify_password(stored_hash, DEFAULT_ADMIN_PASSWORD):
                    default_admin = True
                    results.append("⚠️ Security risk: Admin user still has default password")
                else:
                    results.append("✅ Admin password: Custom (not default)")
            
            # Active sessions
            active_sessions = len(user_auth["tokens"])
            results.append(f"Active sessions: {active_sessions}")
        else:
            results.append("❌ Authentication system: Not initialized properly")
            results.append("   Try restarting the server to initialize authentication")
        
        results.append("")
    
    # Performance issues
    if issue_type in ["performance", "all"]:
        results.append("## Performance Troubleshooting")
        
        system_health = get_system_health()
        
        # Check CPU usage
        cpu_percent = system_health["cpu"]["percent"]
        if cpu_percent > 80:
            results.append(f"❌ CPU usage: {cpu_percent}% (Very High)")
        elif cpu_percent > 50:
            results.append(f"⚠️ CPU usage: {cpu_percent}% (Moderate)")
        else:
            results.append(f"✅ CPU usage: {cpu_percent}% (Good)")
        
        # Check memory usage
        mem_percent = system_health["memory"]["percent"]
        if mem_percent > 90:
            results.append(f"❌ Memory usage: {mem_percent}% (Critical)")
        elif mem_percent > 70:
            results.append(f"⚠️ Memory usage: {mem_percent}% (High)")
        else:
            results.append(f"✅ Memory usage: {mem_percent}% (Good)")
        
        # Check disk space
        disk_percent = system_health["disk"]["percent"]
        if disk_percent > 90:
            results.append(f"❌ Disk usage: {disk_percent}% (Critical)")
        elif disk_percent > 80:
            results.append(f"⚠️ Disk usage: {disk_percent}% (High)")
        else:
            results.append(f"✅ Disk usage: {disk_percent}% (Good)")
        
        # Check log file size
        try:
            log_size = os.path.getsize("coreflux_mcp.log")
            log_size_mb = log_size / (1024 * 1024)
            
            if log_size_mb > 100:
                results.append(f"❌ Log file size: {log_size_mb:.2f} MB (Very Large)")
                results.append("   Consider rotating logs using the `rotate_logs` tool")
            elif log_size_mb > 10:
                results.append(f"⚠️ Log file size: {log_size_mb:.2f} MB (Growing)")
            else:
                results.append(f"✅ Log file size: {log_size_mb:.2f} MB (Good)")
        except Exception:
            results.append("⚠️ Could not check log file size")
        
        results.append("")
    
    # Add summary and recommendations
    results.append("## Summary")
    if connection_status["connected"] and user_auth["users"] and system_health["cpu"]["percent"] < 70:
        results.append("✅ Overall system appears to be healthy")
    else:
        results.append("⚠️ Some issues were detected")
        
    results.append("\n## Recommendations")
    if not connection_status["connected"]:
        results.append("- Fix MQTT connection issues first")
    if "admin" in user_auth["users"] and verify_password(user_auth["users"]["admin"]["password_hash"], DEFAULT_ADMIN_PASSWORD):
        results.append("- Change the default admin password immediately")
    if system_health["disk"]["percent"] > 85:
        results.append("- Free up disk space")
    if os.path.exists("coreflux_mcp.log") and os.path.getsize("coreflux_mcp.log") > 10 * 1024 * 1024:
        results.append("- Rotate log files")
    
    # Return the troubleshooting results
    return "\n".join(results)

if __name__ == "__main__":
    try:
        logger.info("Starting Coreflux MCP Server")
        
        # Run setup assistant if enabled
        if ENABLE_SETUP_ASSISTANT:
            run_setup_assistant()
        
        # Parse command-line arguments
        args = parse_args()
        
        # Initialize MQTT connection
        if not setup_mqtt(args):
            logger.error("Failed to initialize MQTT connection. Exiting.")
            sys.exit(1)
        
        # Initialize authentication system
        init_auth_system()
        
        # Log startup information
        logger.info(f"Server started with client ID: {args.mqtt_client_id}")
        logger.info(f"Connected to MQTT broker at: {args.mqtt_host}:{args.mqtt_port}")
        
        # Run with standard transport
        logger.info("Starting FastMCP server")
        mcp.run()
    except KeyboardInterrupt:
        logger.info("Server shutdown requested by user")
        if mqtt_client:
            mqtt_client.disconnect()
            mqtt_client.loop_stop()
        sys.exit(0)
    except Exception as e:
        logger.critical(f"Unhandled exception: {str(e)}", exc_info=True)
        if mqtt_client:
            mqtt_client.disconnect()
            mqtt_client.loop_stop()
>>>>>>> Stashed changes
        sys.exit(1)