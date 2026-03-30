"""Allow running as: python3 -m brainstem"""
import asyncio

if __name__ == "__main__":
    from brainstem.main import main
    asyncio.run(main())
