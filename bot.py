import os
import sys
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def main():
    logger.info("=== ENVIRONMENT VARIABLES CHECK ===")
    
    telegram_token = os.environ.get('TELEGRAM_TOKEN')
    chat_id = os.environ.get('CHAT_ID')
    alpha_key = os.environ.get('ALPHA_VANTAGE_API_KEY')
    
    logger.info(f"TELEGRAM_TOKEN: {'✓ SET' if telegram_token else '✗ MISSING'}")
    logger.info(f"CHAT_ID: {'✓ SET' if chat_id else '✗ MISSING'}")
    logger.info(f"ALPHA_VANTAGE_API_KEY: {'✓ SET' if alpha_key else '✗ MISSING'}")
    
    if not all([telegram_token, chat_id, alpha_key]):
        logger.error("Missing required environment variables!")
        logger.info("\nPlease set these in Render Dashboard:")
        logger.info("1. Go to your Cron Job")
        logger.info("2. Click 'Environment' tab")
        logger.info("3. Add the following variables:")
        logger.info("   - TELEGRAM_TOKEN")
        logger.info("   - CHAT_ID") 
        logger.info("   - ALPHA_VANTAGE_API_KEY")
        sys.exit(1)
    else:
        logger.info("✅ All environment variables are set correctly!")
        sys.exit(0)

if __name__ == '__main__':
    main()
