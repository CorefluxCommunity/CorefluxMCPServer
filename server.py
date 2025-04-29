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
from parser import process_json_rpc_message
from typing import Optional
import pydantic
from pydantic import BaseModel, validator

# Define a custom NONE logging level (higher than CRITICAL)
NONE_LEVEL = 100  # Higher than CRITICAL (50)
logging.addLevelName(NONE_LEVEL, "NONE")

# Load environment variables from .env file if it exists
load_dotenv()

# Configure logging
def setup_logging(level_name):
    # Special handling for NONE level
    if level_name == "NONE":
        # Disable all logging by setting level to NONE_LEVEL
        level = NONE_LEVEL
    else:
        # Use standard logging levels
        level = getattr(logging, level_name, logging.INFO)
    
    # Create a logger with our app name
    logger = logging.getLogger("CorefluxMCP")
    logger.setLevel(level)
    
    # Remove any existing handlers to avoid duplicates
    for handler in logger.handlers[:]:
        logger.removeHandler(handler)
    
    # Use a format that doesn't conflict with MCP's logging
    formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
    
    # Add handlers with our formatter
    handlers = [
        logging.FileHandler("coreflux_mcp.log"),
        logging.StreamHandler(sys.stderr)
    ]
    
    for handler in handlers:
        handler.setFormatter(formatter)
        handler.setLevel(level)
        logger.addHandler(handler)
    
    # Don't interfere with the root logger, which MCP might use
    logger.propagate = False
        
    return logger

# Parse command-line arguments
def parse_args():
    parser = argparse.ArgumentParser(description="Coreflux MQTT MCP Server")
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
                      choices=["NONE", "DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
                      help="Set logging level (NONE disables all logging)")
    return parser.parse_args()

# Get command line arguments and setup logging
args = parse_args()
logger = setup_logging(args.log_level)

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
mqtt_subscriptions = {}  # Track active subscriptions
mqtt_message_buffer = {}  # Buffer to store received messages

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

def on_disconnect(client, userdata, rc, properties=None, reason_code=0):
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
        # Store message in buffer
        topic = msg.topic
        try:
            payload = msg.payload.decode('utf-8')
        except UnicodeDecodeError:
            payload = str(msg.payload)
        
        # Initialize topic in buffer if it doesn't exist
        if topic not in mqtt_message_buffer:
            mqtt_message_buffer[topic] = []
        
        # Add message with timestamp
        mqtt_message_buffer[topic].append({
            "payload": payload,
            "timestamp": time.time(),
            "qos": msg.qos,
            "retain": msg.retain
        })
        
        # Limit buffer size (keep last 100 messages per topic)
        if len(mqtt_message_buffer[topic]) > 100:
            mqtt_message_buffer[topic] = mqtt_message_buffer[topic][-100:]
        
        # Extract action name from topic
        topic_parts = msg.topic.split('/')
        if len(topic_parts) >= 4 and topic_parts[-1] == "Description":
            action_name = topic_parts[-2]
            
            # Log the raw data for debugging
            payload_raw = msg.payload
            logger.debug(f"Raw message received: {repr(payload_raw)}")
            
            try:
                # Safe decoding of the payload
                payload_str = payload_raw.decode('utf-8').strip()
                
                # Extract description using more robust parsing
                description = extract_description_safely(payload_str)
                
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
            
            except UnicodeDecodeError as e:
                logger.error(f"Failed to decode message payload: {str(e)}")
                return
                
    except Exception as e:
        logger.error(f"Error processing MQTT message: {str(e)}", exc_info=True)

# Helper function for safely extracting descriptions from potentially malformed JSON
def extract_description_safely(payload_str):
    """
    Extract description from a payload string that might be JSON or plain text.
    Implements robust parsing to handle malformed JSON gracefully.
    
    Args:
        payload_str: The string to parse
        
    Returns:
        A string representing the description
    """
    # If it's empty, return empty string
    if not payload_str or not payload_str.strip():
        return ""
        
    # If it doesn't look like JSON, just return the string as-is
    if not (payload_str.strip().startswith('{') and payload_str.strip().endswith('}')):
        return payload_str.strip()
    
    # It looks like JSON, try to parse it properly
    try:
        # Parse as JSON using the parser module
        data = process_json_rpc_message(payload_str)
        
        # If it's a dict with a description field, return that
        if isinstance(data, dict) and 'description' in data:
            return data['description']
        
        # Otherwise, return the whole object as a string
        return payload_str
    
    except Exception as e:
        logger.warning(f"JSON parse error: {str(e)}")
        logger.debug(f"Problematic payload: {payload_str}")
        
        # Return the payload as-is since we couldn't parse it
        return payload_str

def register_dynamic_action_tool(action_name, description):
    try:
        # Skip if already registered
        if action_name in registered_dynamic_tools:
            return
            
        # Create a unique function name for this action
        tool_func_name = f"run_{action_name}"
        
        # Create function in a safer way - avoid direct string interpolation in exec
        # Create the function text with proper escaping for the docstring
        description_safe = description.replace('\\', '\\\\').replace('"', '\\"')
        
        # Define the function code
        func_code = f'''
@mcp.tool()
async def {tool_func_name}(ctx: Context) -> str:
    """Run the {action_name} action: {description_safe}"""
    response = execute_command(f"-runAction {action_name}")
    logger.info(f"Executed action {action_name}")
    return response
'''
        
        # Execute the code in global scope
        exec(func_code, globals())
        
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
        mqtt_client = mqtt.Client(client_id=args.mqtt_client_id, protocol=mqtt.MQTTv5, callback_api_version=mqtt.CallbackAPIVersion.VERSION2)
        
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
            
        except Exception as e:
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
    except Exception as e:
        error_msg = f"MQTT protocol error while executing command: {str(e)}"
        logger.error(error_msg)
        return f"ERROR: {error_msg}"

# region COREFLUX TOOLS

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
    So you are able to create models, actions and rules before adding them. Any 
    logic that you need to implement in the Coreflux MQTT broker you should ask in this tool first.
    
    IMPORTANT: Format all JSON properly, going directly on the first try to simple quoted strings. This is fundamental for the success of the execution!

    Args:
        query: describe what the user wants in a structured way
        context: Additional context or specific requirements (optional)
    
    Returns:
        str: The reply with documentation and LOT code with the potential actions, models, rules or routes.
    """
    if not query or not query.strip():
        error_msg = "Query cannot be empty"
        logger.error(error_msg)
        return f"ERROR: {error_msg}"
        
    api_url = "https://anselmo.coreflux.org/webhook/chat_lot_beta"
    
    # Improved sanitization for JSON safety
    def sanitize_for_json(text):
        if not text:
            return ""
        # Handle non-string input
        if not isinstance(text, str):
            try:
                return str(text)
            except Exception as e:
                logger.warning(f"Failed to convert to string: {e}")
                return ""
        
        # Replace control characters
        for char in ['\b', '\f', '\n', '\r', '\t']:
            text = text.replace(char, ' ')
        
        # Ensure proper escaping of quotes and backslashes for JSON
        return text.replace('\\', '\\\\').replace('"', '\\"')
    
    # Create a proper JSON-RPC compatible payload
    sanitized_query = sanitize_for_json(query)
    sanitized_context = sanitize_for_json(context) if context else ""
    
    # Create payload with proper validation
    try:
        payload = {
            "jsonrpc": "2.0",
            "id": str(uuid.uuid4()),
            "method": "lot_code_generation",
            "params": {
                "query": sanitized_query,
                "context": sanitized_context
            }
        }
        
        # Validate payload is proper JSON before sending
        payload_json = json.dumps(payload, ensure_ascii=False)
        # Verify we can parse it back
        process_json_rpc_message(payload_json)
        
        logger.debug(f"Sending JSON-RPC request: {payload_json[:200]}...")
        logger.info(f"Requesting LOT code generation with query: {sanitized_query[:50]}..." if len(sanitized_query) > 50 else f"Requesting LOT code generation with query: {sanitized_query}")
    except Exception as e:
        error_msg = f"Failed to create valid JSON payload: {str(e)}"
        logger.error(error_msg)
        return f"Error: {error_msg}"
    
    try:
        # Set proper Content-Type header to ensure correct JSON interpretation
        headers = {"Content-Type": "application/json"}
        response = requests.post(api_url, json=payload, headers=headers, timeout=30)
        
        # Debug the raw response
        logger.debug(f"Raw API response status: {response.status_code}")
        logger.debug(f"Raw API response content: {response.text[:200]}..." if len(response.text) > 200 else response.text)
        
        if response.status_code == 200:
            # Process the response with enhanced error handling
            return process_lot_code_response(response.text)
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

def process_lot_code_response(response_text):
    """
    Process the response from the LOT code generation API with robust JSON handling.
    
    Args:
        response_text: The raw API response text
        
    Returns:
        A formatted string with the processed result, or an error message
    """
    if not response_text or not response_text.strip():
        error_msg = "Empty API response"
        logger.error(error_msg)
        return f"Error: {error_msg}"
    
    # Normalize the response text
    cleaned_text = response_text.strip()
    
    try:
        # Try to parse the response using the parser module
        result = process_json_rpc_message(cleaned_text)
        
        # Use schema validation to ensure we have a valid structure
        if not isinstance(result, dict):
            error_msg = "Invalid response format: not a JSON object"
            logger.error(error_msg)
            logger.debug(f"Response: {cleaned_text[:200]}...")
            return f"Error: {error_msg}"
            
        # Extract the result from JSON-RPC style response if needed
        if "result" in result:
            result = result["result"]
            
        # Log success
        logger.info(f"LOT code generation successful")
        
        # Format the response for output
        return format_lot_code_output(result)
        
    except json.JSONDecodeError as e:
        # Log the detailed error
        error_msg = f"Failed to parse API response: {str(e)}"
        logger.error(error_msg)
        logger.debug(f"JSON parse error details: {str(e)}, line: {e.lineno}, col: {e.colno}, pos: {e.pos}")
        logger.debug(f"Problematic response: {cleaned_text[:500]}...")
        
        # Return a user-friendly error message
        return f"Error: Failed to parse the API response. The service may be experiencing issues."
    except Exception as e:
        error_msg = f"Error processing API response: {str(e)}"
        logger.error(error_msg)
        logger.debug(f"Problematic response: {cleaned_text[:500]}...")
        return f"Error: {error_msg}"

def format_lot_code_output(result):
    """
    Format the LOT code generation result for better readability.
    
    Args:
        result: The parsed result object
        
    Returns:
        A formatted string with the processed result
    """
    # Initialize output array
    output = []
    
    # Extract fields with safe access
    def safe_get(obj, key, default=""):
        """Safely get a value from a dictionary"""
        if isinstance(obj, dict) and key in obj:
            value = obj[key]
            return value if value is not None else default
        return default
    
    # Add title if present
    title = safe_get(result, "title")
    if title:
        output.append(f"# {title}")
        output.append("")
    
    # Add description if present
    description = safe_get(result, "description")
    if description:
        output.append(description)
        output.append("")
    
    # Add LOT code if present
    lot_code = safe_get(result, "lot_code")
    if lot_code:
        output.append("```")
        output.append(lot_code)
        output.append("```")
        output.append("")
    
    # Add explanation if present
    explanation = safe_get(result, "explanation")
    if explanation:
        output.append("## Explanation")
        output.append(explanation)
    
    # If we didn't recognize any fields, return the raw object as string
    if not output:
        return f"Received response: {json.dumps(result, indent=2)}"
        
    # Join all parts with newlines and return
    return "\n".join(output)

# endregion

# region MCP RESOURCES

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

# endregion

# region MQTT TOOLS

class MqttMessageModel(BaseModel):
    message: str

    @validator('message', pre=True)
    def ensure_json_string(cls, v):
        import json
        if isinstance(v, dict):
            return json.dumps(v)
        # Validate that the string is valid JSON
        try:
            json.loads(v)
        except Exception:
            raise ValueError("message must be a valid JSON string")
        return v

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

@mcp.tool()
async def mqtt_connect(broker: str, port: int = 1883, username: str = None, password: str = None, 
                      client_id: str = None, use_tls: bool = False, ctx: Context = None) -> str:
    """
    Connect to a specific MQTT broker.
    
    Args:
        broker: The MQTT broker hostname or IP address
        port: The MQTT broker port (default: 1883)
        username: Optional username for authentication
        password: Optional password for authentication
        client_id: Optional client ID (default: auto-generated)
        use_tls: Whether to use TLS encryption (default: False)
        
    Returns:
        A string indicating success or failure of the connection attempt
    """
    global mqtt_client
    
    # Generate client ID if not provided
    if not client_id:
        client_id = f"coreflux-mcp-{uuid.uuid4().hex[:8]}"
    
    # Log the attempt
    logger.info(f"Attempting to connect to MQTT broker at {broker}:{port} with client ID: {client_id}")
    
    try:
        # Create new client
        mqtt_client = mqtt.Client(client_id=client_id, protocol=mqtt.MQTTv5, 
                                 callback_api_version=mqtt.CallbackAPIVersion.VERSION2)
        
        # Set up authentication if provided
        if username and password:
            mqtt_client.username_pw_set(username, password)
            logger.debug(f"Using MQTT authentication with username: {username}")
        
        # Configure TLS if enabled
        if use_tls:
            mqtt_client.tls_set()
            logger.info("TLS configuration enabled for MQTT connection")
        
        # Set callbacks
        mqtt_client.on_connect = on_connect
        mqtt_client.on_message = on_message
        mqtt_client.on_disconnect = on_disconnect
        
        # Connect to broker
        connection_status["last_connection_attempt"] = datetime.now()
        mqtt_client.connect(broker, port, 60)
        mqtt_client.loop_start()
        
        # Wait briefly to check connection status
        max_wait = 3  # seconds
        for _ in range(max_wait * 2):
            if connection_status["connected"]:
                logger.info("MQTT client connected successfully")
                return f"Successfully connected to MQTT broker at {broker}:{port}"
            time.sleep(0.5)
        
        # If we get here, we didn't connect within the timeout
        logger.warning(f"MQTT connection not confirmed after {max_wait} seconds, but loop started")
        return f"Connection attempt completed, but status unclear. Use get_connection_status to verify."
        
    except Exception as e:
        error_msg = f"Failed to connect to MQTT broker: {str(e)}"
        logger.error(error_msg)
        connection_status["last_error"] = str(e)
        return f"ERROR: {error_msg}"

@mcp.tool()
async def mqtt_publish(topic: str, message, qos: int = 0, retain: bool = False, is_json: bool = False, ctx: Context = None) -> str:
    """
    Publish a message to an MQTT topic.
    
    IMPORTANT: Please format the message as a string. If you need to send JSON, please format it as a string with escaped quotes.

    Args:
        topic: The MQTT topic to publish to
        message: The message payload to publish (string or JSON object)
        qos: Quality of Service level (0, 1, or 2)
        retain: Whether the message should be retained by the broker
        is_json: Force message to be treated as JSON (default: auto-detect)
        
    Returns:
        A string confirming successful publication or describing an error
    """
    if not mqtt_client:
        error_msg = "MQTT client not initialized. Use mqtt_connect first."
        logger.error(error_msg)
        return f"ERROR: {error_msg}"
        
    if not connection_status["connected"]:
        error_msg = "MQTT client not connected. Use mqtt_connect first."
        logger.error(error_msg)
        return f"ERROR: {error_msg}"
    
    try:
        # Validate and serialize message as JSON string using Pydantic
        try:
            validated = MqttMessageModel(message=message)
            payload = validated.message
            logger.debug("Message validated and serialized as JSON string")
        except Exception as e:
            error_msg = f"Message validation failed: {e}"
            logger.error(error_msg)
            return f"ERROR: {error_msg}"
        
        # Log the attempt
        logger.info(f"Publishing to topic '{topic}' with QoS {qos}, retain={retain}")
        logger.debug(f"Message payload: {payload[:100]}{'...' if len(payload) > 100 else ''}")
        
        # Publish the message
        result = mqtt_client.publish(topic, payload, qos=qos, retain=retain)
        if result.rc == mqtt.MQTT_ERR_SUCCESS:
            logger.info(f"Successfully published to '{topic}'")
            return f"Message successfully published to topic '{topic}'"
        else:
            error_msg = f"Failed to publish message: {mqtt.error_string(result.rc)}"
            logger.error(error_msg)
            return f"ERROR: {error_msg}"
    except Exception as e:
        error_msg = f"Error while publishing message: {str(e)}"
        logger.error(error_msg)
        return f"ERROR: {error_msg}"

@mcp.tool()
async def mqtt_subscribe(topic: str, qos: int = 0, ctx: Context = None) -> str:
    """
    Subscribe to an MQTT topic.
    
    Args:
        topic: The MQTT topic to subscribe to (can include wildcards # and +)
        qos: Quality of Service level (0, 1, or 2)
        
    Returns:
        A string confirming successful subscription or describing an error
    """
    if not mqtt_client:
        error_msg = "MQTT client not initialized. Use mqtt_connect first."
        logger.error(error_msg)
        return f"ERROR: {error_msg}"
        
    if not connection_status["connected"]:
        error_msg = "MQTT client not connected. Use mqtt_connect first."
        logger.error(error_msg)
        return f"ERROR: {error_msg}"
    
    try:
        # Log the attempt
        logger.info(f"Subscribing to topic '{topic}' with QoS {qos}")
        
        # Subscribe to the topic
        result, mid = mqtt_client.subscribe(topic, qos)
        if result == mqtt.MQTT_ERR_SUCCESS:
            # Track this subscription
            mqtt_subscriptions[topic] = {
                "qos": qos,
                "subscribed_at": datetime.now().isoformat()
            }
            
            logger.info(f"Successfully subscribed to '{topic}'")
            return f"Successfully subscribed to topic '{topic}' with QoS {qos}"
        else:
            error_msg = f"Failed to subscribe to topic: {mqtt.error_string(result)}"
            logger.error(error_msg)
            return f"ERROR: {error_msg}"
    except Exception as e:
        error_msg = f"Error while subscribing: {str(e)}"
        logger.error(error_msg)
        return f"ERROR: {error_msg}"

@mcp.tool()
async def mqtt_unsubscribe(topic: str, ctx: Context = None) -> str:
    """
    Unsubscribe from an MQTT topic.
    
    Args:
        topic: The MQTT topic to unsubscribe from
        
    Returns:
        A string confirming successful unsubscription or describing an error
    """
    if not mqtt_client:
        error_msg = "MQTT client not initialized. Use mqtt_connect first."
        logger.error(error_msg)
        return f"ERROR: {error_msg}"
        
    if not connection_status["connected"]:
        error_msg = "MQTT client not connected. Use mqtt_connect first."
        logger.error(error_msg)
        return f"ERROR: {error_msg}"
    
    try:
        # Log the attempt
        logger.info(f"Unsubscribing from topic '{topic}'")
        
        # Unsubscribe from the topic
        result, mid = mqtt_client.unsubscribe(topic)
        if result == mqtt.MQTT_ERR_SUCCESS:
            # Remove from tracked subscriptions
            if topic in mqtt_subscriptions:
                del mqtt_subscriptions[topic]
                
            logger.info(f"Successfully unsubscribed from '{topic}'")
            return f"Successfully unsubscribed from topic '{topic}'"
        else:
            error_msg = f"Failed to unsubscribe from topic: {mqtt.error_string(result)}"
            logger.error(error_msg)
            return f"ERROR: {error_msg}"
    except Exception as e:
        error_msg = f"Error while unsubscribing: {str(e)}"
        logger.error(error_msg)
        return f"ERROR: {error_msg}"

@mcp.tool()
async def mqtt_read_messages(topic: Optional[str] = None, max_messages: int = 10, clear_buffer: bool = False, ctx: Context = None) -> str:
    """
    Read messages from the MQTT message buffer.
    
    Args:
        topic: The specific topic to read from (None for all topics)
        max_messages: Maximum number of messages to return per topic
        clear_buffer: Whether to clear the message buffer after reading
        
    Returns:
        A formatted string containing the retrieved messages or error information
    """
    if not mqtt_client:
        error_msg = "MQTT client not initialized. Use mqtt_connect first."
        logger.error(error_msg)
        return f"ERROR: {error_msg}"
    
    try:
        if not mqtt_message_buffer:
            logger.info("No messages in buffer")
            return "No messages have been received yet."
        
        # Filter by topic if provided
        topics_to_read = [topic] if topic else list(mqtt_message_buffer.keys())
        
        # Build result
        result = []
        
        for t in topics_to_read:
            if t in mqtt_message_buffer:
                messages = mqtt_message_buffer[t][-max_messages:] if max_messages > 0 else mqtt_message_buffer[t]
                
                # Format messages for this topic
                result.append(f"Topic: {t}")
                result.append(f"Messages: {len(messages)}")
                result.append("-" * 40)
                
                for idx, msg in enumerate(messages):
                    result.append(f"Message {idx+1}:")
                    result.append(f"  Payload: {msg['payload']}")
                    result.append(f"  Timestamp: {datetime.fromtimestamp(msg['timestamp']).isoformat()}")
                    result.append(f"  QoS: {msg['qos']}")
                    result.append(f"  Retain: {msg['retain']}")
                    result.append("-" * 20)
                
                # Clear buffer if requested
                if clear_buffer:
                    mqtt_message_buffer[t] = []
            
        if not result:
            return f"No messages found for topic '{topic}'" if topic else "No messages found"
            
        # Clear all buffer if requested
        if clear_buffer and not topic:
            mqtt_message_buffer.clear()
            
        logger.info(f"Read {len(result)} messages" + (f" from topic '{topic}'" if topic else ""))
        return "\n".join(result)
    except Exception as e:
        error_msg = f"Error while reading messages: {str(e)}"
        logger.error(error_msg)
        return f"ERROR: {error_msg}"

@mcp.tool()
async def mqtt_list_subscriptions(ctx: Context = None) -> str:
    """
    List all active MQTT subscriptions.
    
    Returns:
        A formatted string listing all active subscriptions
    """
    if not mqtt_client:
        error_msg = "MQTT client not initialized. Use mqtt_connect first."
        logger.error(error_msg)
        return f"ERROR: {error_msg}"
        
    if not connection_status["connected"]:
        error_msg = "MQTT client not connected. Use mqtt_connect first."
        logger.error(error_msg)
        return f"ERROR: {error_msg}"
    
    if not mqtt_subscriptions:
        logger.info("No active subscriptions")
        return "No active subscriptions"
        
    # Format the result
    result = ["Active MQTT Subscriptions:"]
    for topic, details in mqtt_subscriptions.items():
        result.append(f"- Topic: {topic}")
        result.append(f"  QoS: {details['qos']}")
        result.append(f"  Subscribed at: {details['subscribed_at']}")
        
    logger.info(f"Listed {len(mqtt_subscriptions)} active subscriptions")
    return "\n".join(result)

@mcp.tool()
async def mqtt_read_topic_once(topic: str, timeout: float = 5.0, qos: int = 0, ctx: Context = None) -> str:
    """
    One-off MQTT topic read: Subscribes to a topic, waits for a single message, then unsubscribes and returns the message.
    This is for single, immediate reads (not continuous monitoring).

    Args:
        topic: The MQTT topic to read from
        timeout: Maximum time (in seconds) to wait for a message (default: 5.0)
        qos: Quality of Service level (0, 1, or 2)
    Returns:
        The first received message payload, or an error/timeout message
    """
    if not mqtt_client:
        error_msg = "MQTT client not initialized. Use mqtt_connect first."
        logger.error(error_msg)
        return f"ERROR: {error_msg}"
    if not connection_status["connected"]:
        error_msg = "MQTT client not connected. Use mqtt_connect first."
        logger.error(error_msg)
        return f"ERROR: {error_msg}"
    event = None
    message_holder = {}
    import threading
    def on_temp_message(client, userdata, msg):
        try:
            message_holder["payload"] = msg.payload.decode("utf-8", errors="replace")
            message_holder["timestamp"] = time.time()
            message_holder["qos"] = msg.qos
            message_holder["retain"] = msg.retain
        except Exception as e:
            message_holder["payload"] = str(msg.payload)
        if event:
            event.set()
    event = threading.Event()
    # Temporarily add a message callback for this topic
    mqtt_client.message_callback_add(topic, on_temp_message)
    try:
        logger.info(f"[One-off] Subscribing to topic '{topic}' for one message (timeout {timeout}s)")
        result, mid = mqtt_client.subscribe(topic, qos)
        if result != mqtt.MQTT_ERR_SUCCESS:
            return f"ERROR: Failed to subscribe: {mqtt.error_string(result)}"
        # Wait for a message or timeout
        got_message = event.wait(timeout)
        mqtt_client.unsubscribe(topic)
        mqtt_client.message_callback_remove(topic)
        if got_message and "payload" in message_holder:
            return f"Received message on '{topic}': {message_holder['payload']}"
        else:
            return f"Timeout: No message received on '{topic}' within {timeout} seconds."
    except Exception as e:
        logger.error(f"Error in mqtt_read_topic_once: {e}")
        return f"ERROR: {e}"

@mcp.tool()
async def mqtt_monitor_topic(topic: str, qos: int = 0, ctx: Context = None) -> str:
    """
    Monitor MQTT topic: Subscribes to a topic for continuous monitoring. Messages will be buffered and can be read using mqtt_read_messages.
    This tool does NOT unsubscribe automatically. Use mqtt_unsubscribe to stop monitoring.

    Args:
        topic: The MQTT topic to monitor
        qos: Quality of Service level (0, 1, or 2)
    Returns:
        Confirmation of subscription or error message
    """
    if not mqtt_client:
        error_msg = "MQTT client not initialized. Use mqtt_connect first."
        logger.error(error_msg)
        return f"ERROR: {error_msg}"
    if not connection_status["connected"]:
        error_msg = "MQTT client not connected. Use mqtt_connect first."
        logger.error(error_msg)
        return f"ERROR: {error_msg}"
    try:
        logger.info(f"[Monitor] Subscribing to topic '{topic}' for monitoring")
        result, mid = mqtt_client.subscribe(topic, qos)
        if result == mqtt.MQTT_ERR_SUCCESS:
            mqtt_subscriptions[topic] = {
                "qos": qos,
                "subscribed_at": datetime.now().isoformat()
            }
            return f"Now monitoring topic '{topic}' (QoS {qos}). Use mqtt_read_messages to view messages."
        else:
            return f"ERROR: Failed to subscribe: {mqtt.error_string(result)}"
    except Exception as e:
        logger.error(f"Error in mqtt_monitor_topic: {e}")
        return f"ERROR: {e}"

# endregion

if __name__ == "__main__":
    try:
        logger.info("Starting Coreflux MQTT MCP Server")
        
        # Parse command-line arguments
        args = parse_args()
        
        # Initialize MQTT connection
        if not setup_mqtt(args):
            logger.error("Failed to initialize MQTT connection. Exiting.")
            logger.error("Failed to initialize MQTT connection. Run setup_assistant.py to configure your connection.")
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