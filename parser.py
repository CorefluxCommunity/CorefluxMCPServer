"""
Utility functions for parsing JSON-RPC messages

This module provides functions to handle malformed JSON-RPC messages
and extract valid JSON objects or arrays from them.
"""
import json
import logging
import re

# Set up logger
logger = logging.getLogger("CorefluxMCP.JsonParser")

def process_json_rpc_message(message: str) -> dict:
    """
    Process a potentially malformed JSON-RPC message and extract a valid JSON object or array
    
    Args:
        message: The message string to process
        
    Returns:
        The parsed JSON object or array
        
    Raises:
        Exception: If no valid JSON can be extracted or parsed
    """
    try:
        # Remove special characters like BOM and trim whitespace
        cleaned_message = message.strip()
        if cleaned_message.startswith('\ufeff'):
            cleaned_message = cleaned_message[1:]
            
        # Log the message for debugging
        logger.debug(f"Processing message: {cleaned_message[:50]}...")
        
        # Fix for "Unexpected non-whitespace character after JSON at position 4"
        # This happens when there are extra characters after a valid JSON object
        # Try to find a valid JSON object or array at the beginning of the string
        json_match = re.match(r'^(\{.*\}|\[.*\])(?:\s*|$)', cleaned_message, re.DOTALL)
        if json_match and json_match.group(1):
            try:
                # If we can parse this directly, return it
                parsed = json.loads(json_match.group(1))
                logger.debug(f"Successfully parsed JSON directly: {json_match.group(1)[:50]}...")
                return parsed
            except json.JSONDecodeError as e:
                # If direct parsing fails, continue with the original algorithm
                logger.debug(f"Direct parsing failed, continuing with original algorithm: {e}")
        
        # Find the start of the JSON object or array (whichever comes first)
        object_start = cleaned_message.find('{')
        array_start = cleaned_message.find('[')
        
        if object_start >= 0 and (array_start < 0 or object_start < array_start):
            json_start_index = object_start
            is_object = True
        elif array_start >= 0:
            json_start_index = array_start
            is_object = False
        else:
            raise Exception('No JSON object or array found in the message')
        
        # Determine if we're dealing with an object or array
        open_char = '{' if is_object else '['
        close_char = '}' if is_object else ']'
        depth = 0
        end_index = -1
        
        # Use balanced bracket matching to find the end of the JSON
        for i in range(json_start_index, len(cleaned_message)):
            if cleaned_message[i] == open_char:
                depth += 1
            elif cleaned_message[i] == close_char:
                depth -= 1
                if depth == 0:
                    end_index = i + 1
                    break
        
        if end_index == -1:
            raise Exception('Unbalanced JSON in the message')
        
        # Extract and parse the JSON string
        json_string = cleaned_message[json_start_index:end_index]
        try:
            return json.loads(json_string)
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse extracted JSON: {e}")
            logger.debug(f"Extracted JSON string: \"{json_string}\"")
            raise e
            
    except Exception as error:
        logger.error(f"Error parsing JSON-RPC message: {error}")
        logger.debug(f"Problematic message: \"{message}\"")
        raise error