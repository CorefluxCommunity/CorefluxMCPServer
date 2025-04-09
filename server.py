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

# Tools for Coreflux commands
@mcp.tool()
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
async def remove_rule(rule_name: str, ctx: Context) -> str:
    """Remove a permission rule from Coreflux"""
    if not rule_name or not rule_name.strip():
        error_msg = "Rule name cannot be empty"
        logger.error(error_msg)
        return f"ERROR: {error_msg}"
        
    logger.info(f"Removing rule: {rule_name}")
    return execute_command(f"-removeRule {rule_name}")

@mcp.tool()
async def add_route(ctx: Context) -> str:
    """Add a new route connection"""
    logger.info("Adding new route")
    return execute_command("-addRoute")

@mcp.tool()
async def remove_route(route_id: str, ctx: Context) -> str:
    """Remove a route connection"""
    if not route_id or not route_id.strip():
        error_msg = "Route ID cannot be empty"
        logger.error(error_msg)
        return f"ERROR: {error_msg}"
        
    logger.info(f"Removing route: {route_id}")
    return execute_command(f"-removeRoute {route_id}")

@mcp.tool()
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
async def remove_model(model_name: str, ctx: Context) -> str:
    """Remove a model structure from Coreflux"""
    if not model_name or not model_name.strip():
        error_msg = "Model name cannot be empty"
        logger.error(error_msg)
        return f"ERROR: {error_msg}"
        
    logger.info(f"Removing model: {model_name}")
    return execute_command(f"-removeModel {model_name}")

@mcp.tool()
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
async def remove_action(action_name: str, ctx: Context) -> str:
    """Remove an action event/function from Coreflux"""
    if not action_name or not action_name.strip():
        error_msg = "Action name cannot be empty"
        logger.error(error_msg)
        return f"ERROR: {error_msg}"
        
    logger.info(f"Removing action: {action_name}")
    return execute_command(f"-removeAction {action_name}")

@mcp.tool()
async def run_action(action_name: str, ctx: Context) -> str:
    """Run an action event/function in Coreflux"""
    if not action_name or not action_name.strip():
        error_msg = "Action name cannot be empty"
        logger.error(error_msg)
        return f"ERROR: {error_msg}"
        
    logger.info(f"Running action: {action_name}")
    return execute_command(f"-runAction {action_name}")

@mcp.tool()
async def remove_all_models(ctx: Context) -> str:
    """Remove all models from Coreflux"""
    logger.warning("Removing ALL models - this is a destructive operation")
    return execute_command("-removeAllModels")

@mcp.tool()
async def remove_all_actions(ctx: Context) -> str:
    """Remove all actions from Coreflux"""
    logger.warning("Removing ALL actions - this is a destructive operation")
    return execute_command("-removeAllActions")

@mcp.tool()
async def remove_all_routes(ctx: Context) -> str:
    """Remove all routes from Coreflux"""
    logger.warning("Removing ALL routes - this is a destructive operation")
    return execute_command("-removeAllRoutes")

@mcp.tool()
async def lot_diagnostic(diagnostic_value: str, ctx: Context) -> str:
    """Change the LOT Diagnostic"""
    if not diagnostic_value or not diagnostic_value.strip():
        error_msg = "Diagnostic value cannot be empty"
        logger.error(error_msg)
        return f"ERROR: {error_msg}"
        
    logger.info(f"Setting LOT diagnostic to: {diagnostic_value}")
    return execute_command(f"-lotDiagnostic {diagnostic_value}")

@mcp.tool()
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
                result = response.json()
                logger.info("LOT code generation successful")
                return json.dumps(result, indent=2)
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


if __name__ == "__main__":
    try:
        logger.info("Starting Coreflux MCP Server")
        
        # Parse command-line arguments
        args = parse_args()
        
        # Initialize MQTT connection
        if not setup_mqtt(args):
            logger.error("Failed to initialize MQTT connection. Exiting.")
            sys.exit(1)
        
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
        sys.exit(1) 