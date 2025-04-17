"""
Conversation thread management system for AgentPress.

This module provides comprehensive conversation management, including:
- Thread creation and persistence
- Message handling with support for text and images
- Tool registration and execution
- LLM interaction with streaming support
- Error handling and cleanup
- Context summarization to manage token limits
"""

import json
from typing import List, Dict, Any, Optional, Type, Union, AsyncGenerator, Literal
from services.llm import make_llm_api_call
from agentpress.tool import Tool
from agentpress.tool_registry import ToolRegistry
from agentpress.context_manager import ContextManager
from agentpress.response_processor import (
    ResponseProcessor, 
    ProcessorConfig    
)
from services.supabase import DBConnection
from utils.logger import logger

# Type alias for tool choice
ToolChoice = Literal["auto", "required", "none"]

class ThreadManager:
    """Manages conversation threads with LLM models and tool execution.
    
    Provides comprehensive conversation management, handling message threading,
    tool registration, and LLM interactions with support for both standard and
    XML-based tool execution patterns.
    """

    def __init__(self):
        """Initialize ThreadManager.
    
        """
        self.db = DBConnection()
        self.tool_registry = ToolRegistry()
        self.response_processor = ResponseProcessor(
            tool_registry=self.tool_registry,
            add_message_callback=self.add_message
        )
        self.context_manager = ContextManager()

    def add_tool(self, tool_class: Type[Tool], function_names: Optional[List[str]] = None, **kwargs):
        """Add a tool to the ThreadManager."""
        self.tool_registry.register_tool(tool_class, function_names, **kwargs)

    async def add_message(
        self, 
        thread_id: str, 
        type: str, 
        content: Union[Dict[str, Any], List[Any], str], 
        is_llm_message: bool = False,
        metadata: Optional[Dict[str, Any]] = None
    ):
        """Add a message to the thread in the database.

        Args:
            thread_id: The ID of the thread to add the message to.
            type: The type of the message (e.g., 'text', 'image_url', 'tool_call', 'tool', 'user', 'assistant').
            content: The content of the message. Can be a dictionary, list, or string.
                     It will be stored as JSONB in the database.
            is_llm_message: Flag indicating if the message originated from the LLM.
                            Defaults to False (user message).
            metadata: Optional dictionary for additional message metadata.
                      Defaults to None, stored as an empty JSONB object if None.
        """
        logger.debug(f"Adding message of type '{type}' to thread {thread_id}")
        client = await self.db.client
        
        # Prepare data for insertion
        data_to_insert = {
            'thread_id': thread_id,
            'type': type,
            'content': json.dumps(content) if isinstance(content, (dict, list)) else content,
            'is_llm_message': is_llm_message,
            'metadata': json.dumps(metadata or {}), # Ensure metadata is always a JSON object
        }
        
        try:
            result = await client.table('messages').insert(data_to_insert).execute()
            logger.info(f"Successfully added message to thread {thread_id}")
        except Exception as e:
            logger.error(f"Failed to add message to thread {thread_id}: {str(e)}", exc_info=True)
            raise

    async def get_llm_messages(self, thread_id: str) -> List[Dict[str, Any]]:
        """Get all messages for a thread.
        
        This method uses the SQL function which handles context truncation
        by considering summary messages.
        
        Args:
            thread_id: The ID of the thread to get messages for.
            
        Returns:
            List of message objects.
        """
        logger.debug(f"Getting messages for thread {thread_id}")
        client = await self.db.client
        
        try:
            result = await client.rpc('get_llm_formatted_messages', {'p_thread_id': thread_id}).execute()
            
            # Parse the returned data which might be stringified JSON
            if not result.data:
                return []
                
            # Return properly parsed JSON objects
            messages = []
            for item in result.data:
                if isinstance(item, str):
                    try:
                        parsed_item = json.loads(item)
                        messages.append(parsed_item)
                    except json.JSONDecodeError:
                        logger.error(f"Failed to parse message: {item}")
                else:
                    messages.append(item)

            # Ensure tool_calls have properly formatted function arguments
            for message in messages:
                if message.get('tool_calls'):
                    for tool_call in message['tool_calls']:
                        if isinstance(tool_call, dict) and 'function' in tool_call:
                            # Ensure function.arguments is a string
                            if 'arguments' in tool_call['function'] and not isinstance(tool_call['function']['arguments'], str):
                                # Log and fix the issue
                                # logger.warning(f"Found non-string arguments in tool_call, converting to string")
                                tool_call['function']['arguments'] = json.dumps(tool_call['function']['arguments'])

            return messages
            
        except Exception as e:
            logger.error(f"Failed to get messages for thread {thread_id}: {str(e)}", exc_info=True)
            return []

    async def run_thread(
        self,
        thread_id: str,
        system_prompt: Dict[str, Any],
        stream: bool = True,
        temporary_message: Optional[Dict[str, Any]] = None,
        llm_model: str = "gpt-4o",
        llm_temperature: float = 0,
        llm_max_tokens: Optional[int] = None,
        processor_config: Optional[ProcessorConfig] = None,
        tool_choice: ToolChoice = "auto",
        native_max_auto_continues: int = 25,
        max_xml_tool_calls: int = 0,
        include_xml_examples: bool = False,
        enable_thinking: Optional[bool] = False, # Add enable_thinking parameter
        reasoning_effort: Optional[str] = 'low' # Add reasoning_effort parameter
    ) -> Union[Dict[str, Any], AsyncGenerator]:
        """Run a conversation thread with LLM integration and tool execution.
        Args:
            thread_id: The ID of the thread to run
            system_prompt: System message to set the assistant's behavior
            stream: Use streaming API for the LLM response
            temporary_message: Optional temporary user message for this run only
            llm_model: The name of the LLM model to use
            llm_temperature: Temperature parameter for response randomness (0-1)
            llm_max_tokens: Maximum tokens in the LLM response
            processor_config: Configuration for the response processor
            tool_choice: Tool choice preference ("auto", "required", "none")
            native_max_auto_continues: Maximum number of automatic continuations when 
                                      finish_reason="tool_calls" (0 disables auto-continue)
            max_xml_tool_calls: Maximum number of XML tool calls to allow (0 = no limit)
            include_xml_examples: Whether to include XML tool examples in the system prompt
            
        Returns:
            An async generator yielding response chunks or error dict
        """
        
        logger.info(f"Starting thread execution for thread {thread_id}")
        logger.debug(f"Parameters: model={llm_model}, temperature={llm_temperature}, max_tokens={llm_max_tokens}")
        logger.debug(f"Auto-continue: max={native_max_auto_continues}, XML tool limit={max_xml_tool_calls}")
        
        # Control whether we need to auto-continue due to tool_calls finish reason
        auto_continue = True
        auto_continue_count = 0
        
        # Define inner function to handle a single run
        async def _run_once(temp_msg=None):
            try:
                # Ensure processor_config is available in this scope
                nonlocal processor_config
                
                # Use a default config if none was provided
                if processor_config is None:
                    processor_config = ProcessorConfig()
                
                # Apply max_xml_tool_calls if specified and not already set
                if max_xml_tool_calls > 0:
                    processor_config.max_xml_tool_calls = max_xml_tool_calls
                
                # 1. Get messages from thread for LLM call
                messages = await self.get_llm_messages(thread_id)
                
                # 2. Check token count before proceeding
                # Use litellm to count tokens in the messages
                token_count = 0
                try:
                    from litellm import token_counter
                    token_count = token_counter(model=llm_model, messages=[system_prompt] + messages)
                    token_threshold = self.context_manager.token_threshold
                    logger.info(f"Thread {thread_id} token count: {token_count}/{token_threshold} ({(token_count/token_threshold)*100:.1f}%)")
                    
                    # If we're over the threshold, summarize the thread
                    if token_count >= token_threshold:
                        logger.info(f"Thread token count ({token_count}) exceeds threshold ({token_threshold}), summarizing...")
                        
                        # Create summary using context manager
                        summarized = await self.context_manager.check_and_summarize_if_needed(
                            thread_id=thread_id,
                            add_message_callback=self.add_message,
                            model=llm_model,
                            force=True  # Force summarization
                        )
                        
                        if summarized:
                            # If summarization was successful, get the updated messages 
                            # This will now include the summary message and only messages after it
                            logger.info("Summarization complete, fetching updated messages with summary")
                            messages = await self.get_llm_messages(thread_id)
                            # Recount tokens after summarization
                            new_token_count = token_counter(model=llm_model, messages=[system_prompt] + messages)
                            logger.info(f"After summarization: token count reduced from {token_count} to {new_token_count}")
                        else:
                            logger.warning("Summarization failed or wasn't needed - proceeding with original messages")
                except Exception as e:
                    logger.error(f"Error counting tokens or summarizing: {str(e)}")
                
                # 3. Prepare messages for LLM call + add temporary message if it exists
                # Start with the base system prompt content
                current_system_prompt_content = system_prompt['content']

                # Conditionally add XML examples if requested
                if include_xml_examples:
                    xml_examples_dict = self.tool_registry.get_xml_examples()
                    if xml_examples_dict:
                        xml_examples_str = "\n".join(xml_examples_dict.values())
                        current_system_prompt_content += f"\n\n<tool_examples>\n{xml_examples_str}\n</tool_examples>"
                        logger.debug("Added XML examples to system prompt.")

                # Create the final system prompt object for this run
                current_system_prompt = {"role": "system", "content": current_system_prompt_content}

                # Prepare messages for LLM call
                prepared_messages = [current_system_prompt]
                
                # Find the last user message index
                last_user_index = -1
                for i, msg in enumerate(messages):
                    if msg.get('role') == 'user':
                        last_user_index = i
                
                # Insert temporary message before the last user message if it exists
                if temp_msg and last_user_index >= 0:
                    prepared_messages.extend(messages[:last_user_index])
                    prepared_messages.append(temp_msg)
                    prepared_messages.extend(messages[last_user_index:])
                    logger.debug("Added temporary message before the last user message")
                else:
                    # If no user message or no temporary message, just add all messages
                    prepared_messages.extend(messages)
                    if temp_msg:
                        prepared_messages.append(temp_msg)
                        logger.debug("Added temporary message to the end of prepared messages")

                # 4. Create or use processor config - this is now redundant since we handle it above
                # but kept for consistency and clarity
                logger.debug(f"Processor config: XML={processor_config.xml_tool_calling}, Native={processor_config.native_tool_calling}, " 
                       f"Execute tools={processor_config.execute_tools}, Strategy={processor_config.tool_execution_strategy}, "
                       f"XML limit={processor_config.max_xml_tool_calls}")

                # 5. Prepare tools for LLM call
                openapi_tool_schemas = None
                if processor_config.native_tool_calling:
                    openapi_tool_schemas = self.tool_registry.get_openapi_schemas()
                    logger.debug(f"Retrieved {len(openapi_tool_schemas) if openapi_tool_schemas else 0} OpenAPI tool schemas")

                # 6. Make LLM API call
                logger.debug("Making LLM API call")
                try:
                    llm_response = await make_llm_api_call(
                        prepared_messages,
                        llm_model,
                        temperature=llm_temperature,
                        max_tokens=llm_max_tokens,
                        tools=openapi_tool_schemas,
                        tool_choice=tool_choice if processor_config.native_tool_calling else None,
                        stream=stream,
                        enable_thinking=enable_thinking, # Pass enable_thinking
                        reasoning_effort=reasoning_effort # Pass reasoning_effort
                    )
                    logger.debug("Successfully received raw LLM API response stream/object")

                except Exception as e:
                    logger.error(f"Failed to make LLM API call: {str(e)}", exc_info=True)
                    raise

                # 7. Process LLM response using the ResponseProcessor
                if stream:
                    logger.debug("Processing streaming response")
                    response_generator = self.response_processor.process_streaming_response(
                        llm_response=llm_response,
                        thread_id=thread_id,
                        prompt_messages=prepared_messages,
                        llm_model=llm_model,
                        config=processor_config
                    )
                    
                    return response_generator
                else:
                    logger.debug("Processing non-streaming response")
                    # Return the async generator directly, don't await it
                    response_generator = self.response_processor.process_non_streaming_response(
                        llm_response=llm_response,
                        thread_id=thread_id,
                        prompt_messages=prepared_messages,
                        llm_model=llm_model,
                        config=processor_config
                    )
                    return response_generator # Return the generator
              
            except Exception as e:
                logger.error(f"Error in _run_once: {str(e)}", exc_info=True)
                # For generators, we need to yield an error structure if returning a generator is expected
                async def error_generator():
                    yield {
                        "type": "error",
                        "message": f"Error during LLM call or setup: {str(e)}"
                    }
                return error_generator()
        
        # Define a wrapper generator that handles auto-continue logic
        async def auto_continue_wrapper():
            nonlocal auto_continue, auto_continue_count, temporary_message
            
            current_temp_message = temporary_message # Use a local copy for the first run
            
            while auto_continue and (native_max_auto_continues == 0 or auto_continue_count < native_max_auto_continues):
                # Reset auto_continue for this iteration
                auto_continue = False
                
                # Run the thread once
                # Pass current_temp_message, which is only set for the first iteration
                response_gen = await _run_once(temp_msg=current_temp_message) 
                
                # Clear the temporary message after the first run
                current_temp_message = None 
                
                # Handle error responses (checking if it's an error dict, which _run_once might return directly)
                if isinstance(response_gen, dict) and response_gen.get("status") == "error":
                    yield response_gen
                    return
                    
                # Check if it's the error generator from _run_once exception handling
                # Need a way to check if it's the specific error generator or just inspect the first item
                first_chunk = None
                try:
                    first_chunk = await anext(response_gen)
                except StopAsyncIteration:
                    # Empty generator, possibly due to an issue before yielding.
                    logger.warning("Response generator was empty.")
                    break 
                except Exception as e:
                    logger.error(f"Error getting first chunk from generator: {e}")
                    yield {"type": "error", "message": f"Error processing response: {e}"}
                    break

                if first_chunk and first_chunk.get('type') == 'error' and "Error during LLM call" in first_chunk.get('message', ''):
                    yield first_chunk
                    return # Stop processing if setup failed

                # Yield the first chunk if it wasn't an error
                if first_chunk:
                     yield first_chunk
                
                # Process remaining chunks
                async for chunk in response_gen:
                    # Check if this is a finish reason chunk with tool_calls or xml_tool_limit_reached
                    if chunk.get('type') == 'finish':
                        finish_reason = chunk.get('finish_reason')
                        if finish_reason == 'tool_calls':
                            # Only auto-continue if enabled (max > 0)
                            if native_max_auto_continues > 0:
                                logger.info(f"Detected finish_reason='tool_calls', auto-continuing ({auto_continue_count + 1}/{native_max_auto_continues})")
                                auto_continue = True
                                auto_continue_count += 1
                                # Don't yield the finish chunk to avoid confusing the client during auto-continue
                                continue 
                        elif finish_reason == 'xml_tool_limit_reached':
                            # Don't auto-continue if XML tool limit was reached
                            logger.info(f"Detected finish_reason='xml_tool_limit_reached', stopping auto-continue")
                            auto_continue = False
                            # Still yield the chunk to inform the client
                        
                        # Yield other finish reasons normally
                    
                    # Yield the chunk normally
                    yield chunk
                
                # If not auto-continuing, we're done with the loop
                if not auto_continue:
                    break
                
            # If we've reached the max auto-continues, log a warning
            if auto_continue and auto_continue_count >= native_max_auto_continues:
                logger.warning(f"Reached maximum auto-continue limit ({native_max_auto_continues}), stopping.")
                yield {
                    "type": "content", 
                    "content": f"\n[Agent reached maximum auto-continue limit of {native_max_auto_continues}]"
                }
        
        # If auto-continue is disabled (max=0), just run once
        if native_max_auto_continues == 0:
            logger.info("Auto-continue is disabled (native_max_auto_continues=0)")
            return await _run_once(temporary_message)
        
        # Otherwise return the auto-continue wrapper generator
        return auto_continue_wrapper()
