from __future__ import annotations

import asyncio
import logging
import os

from dotenv import load_dotenv

from agent_runtime.gateway.service import BusinessAssistantService
from agent_runtime.gateway.wecom_gateway import WeComGateway
from agent_runtime.logging_utils import setup_logging


logger = logging.getLogger(__name__)

   



def main():
    load_dotenv()
    setup_logging(
        level=os.getenv("LOG_LEVEL", "INFO"),
        log_dir=os.getenv("LOG_DIR", "logs"),
    )
    logger.info("agent_runtime_starting gateway=wecom")
    service = BusinessAssistantService()
    wecom = WeComGateway(service.handle)
    asyncio.run(wecom.run_forever())



if __name__ == "__main__":
    main()
