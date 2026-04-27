"""
Main Entry Point for the Veterinary LLM Pipeline.

Design Decisions:
- Why we use python-dotenv for security: In software, it's dangerous to put secret passwords 
  (like API keys for AI models) directly into the code. The python-dotenv library lets us load 
  these secrets from a hidden '.env' file that never gets shared on the internet.
"""
import os
from dotenv import load_dotenv

def main():
    """
    Starts up the pipeline and prepares the environment.
    """
    # Load our secret keys from the hidden .env file into the computer's memory
    load_dotenv()
    
    # Print a friendly message so we know the program started successfully
    print('Veterinary LLM Pipeline Initialized.')

# This special check ensures our main() function only runs if we run this script directly,
# rather than if another script tries to import it.
if __name__ == "__main__":
    main()
