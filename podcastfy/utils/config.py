"""
Configuration Module

This module handles the loading and management of configuration settings for the Podcastfy application.
It uses environment variables to securely store and access API keys and other sensitive information,
and a YAML file for non-sensitive configuration settings.
"""

import os
from dotenv import load_dotenv
from typing import Any, Dict, Optional
import yaml

class Config:
	def __init__(self):
		"""
		Initialize the Config class by loading environment variables and YAML configuration.
		"""
		load_dotenv()
		
		# Load API keys from .env
		self.JINA_API_KEY: str = os.getenv("JINA_API_KEY", "")
		self.GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "")
		self.OPENAI_API_KEY: str = os.getenv("OPENAI_API_KEY", "")
		self.ELEVENLABS_API_KEY: str = os.getenv("ELEVENLABS_API_KEY", "")
		
		# Load other settings from config.yaml
		with open('config.yaml', 'r') as file:
			self.config: Dict[str, Any] = yaml.safe_load(file)
		
		# Set attributes based on YAML config
		self.TRANSCRIPT_OUTPUT_DIR: str = self.config['output_directories']['transcripts']
		self.AUDIO_OUTPUT_DIR: str = self.config['output_directories']['audio']
		
		# Ensure output directories exist
		os.makedirs(self.TRANSCRIPT_OUTPUT_DIR, exist_ok=True)
		os.makedirs(self.AUDIO_OUTPUT_DIR, exist_ok=True)

	def get(self, key: str, default: Optional[Any] = None) -> Any:
		"""
		Get a configuration value by key.

		Args:
			key (str): The configuration key to retrieve.
			default (Optional[Any]): The default value if the key is not found.

		Returns:
			Any: The value associated with the key, or the default value if not found.
		"""
		return self.config.get(key, default)

def load_config() -> Config:
	"""
	Load and return a Config instance.

	Returns:
		Config: An instance of the Config class.
	"""
	return Config()

def main(seed: int = 42) -> None:
	"""
	Test the Config class and print configuration status.

	Args:
		seed (int): Random seed for reproducibility. Defaults to 42.
	"""
	# Create an instance of the Config class
	config = load_config()
	
	# Test each configuration value
	print("Testing Config class:")
	print(f"EMAIL_ADDRESS: {'Set' if config.get('EMAIL_ADDRESS') else 'Not set'}")
	print(f"EMAIL_PASSWORD: {'Set' if config.get('EMAIL_PASSWORD') else 'Not set'}")
	print(f"JINA_API_KEY: {'Set' if config.get('JINA_API_KEY') else 'Not set'}")
	print(f"GEMINI_API_KEY: {'Set' if config.get('GEMINI_API_KEY') else 'Not set'}")
	print(f"ELEVENLABS_API_KEY: {'Set' if config.get('ELEVENLABS_API_KEY') else 'Not set'}")
	print(f"OPENAI_API_KEY: {'Set' if config.get('OPENAI_API_KEY') else 'Not set'}")

	# Print a warning for any missing configuration
	missing_config = []
	for key in ['EMAIL_ADDRESS', 'EMAIL_PASSWORD', 'JINA_API_KEY', 'GEMINI_API_KEY', 'ELEVENLABS_API_KEY', 'OPENAI_API_KEY']:
		if not config.get(key):
			missing_config.append(key)

	if missing_config:
		print("\nWarning: The following configuration values are missing:")
		for config_name in missing_config:
			print(f"- {config_name}")
		print("Please ensure these are set in your .env file.")
	else:
		print("\nAll configuration values are set.")

	# Test the get method with a default value
	print(f"\nTesting get method with default value:")
	print(f"NON_EXISTENT_KEY: {config.get('NON_EXISTENT_KEY', 'Default Value')}")

if __name__ == "__main__":
	main()