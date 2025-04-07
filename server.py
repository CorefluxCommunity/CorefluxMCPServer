from mcp.server.fastmcp import FastMCP, Context
import os
import paho.mqtt.client as mqtt
import uuid
import argparse
import requests
import json
import logging
import time
import sys
from typing import Dict, Any, Optional

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler('coreflux_mcp.log')
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
    try:
        return execute_command(f"-runAction {action_name}")
    except Exception as e:
        error_msg = f"Error running action: {str(e)}"
        logger.error(error_msg)
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
            "mqtt_connected": mqtt_connection_status["connected"],
            "last_error": mqtt_connection_status["last_error"],
            "last_connection_attempt": mqtt_connection_status["last_attempt"],
            "reconnect_count": mqtt_connection_status["reconnect_count"],
            "discovered_actions_count": len(discovered_actions),
            "registered_tools_count": len(registered_dynamic_tools)
        }
        
        return json.dumps(status, indent=2)
    except Exception as e:
        error_msg = f"Error checking MQTT status: {str(e)}"
        logger.error(error_msg)
        return error_msg

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
    if not setup_mqtt(args):
        logger.error("Failed to initialize MQTT connection, continuing with limited functionality")
    
    # Run with standard transport
    try:
        logger.info("Starting FastMCP server")
        mcp.run()
    except Exception as e:
        logger.critical(f"Error running FastMCP server: {str(e)}")
        sys.exit(1) 