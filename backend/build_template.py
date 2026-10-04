"""Build the E2B template once: uv run --project backend backend/build_template.py."""

import asyncio

from e2b import default_build_logger

from config import Settings
from harness import e2b_client


async def main():
    settings = Settings()
    if not settings.e2b_api_key:
        raise SystemExit("Set E2B_API_KEY in backend/.env first.")
    client = e2b_client(settings)
    template = (
        client.AsyncTemplate()
        .from_image(settings.e2b_base_image)
        .set_user("root")
        .make_dir("/workspace", user="root")
        .set_workdir("/workspace")
    )
    result = await client.AsyncTemplate.build(
        template,
        name=settings.e2b_template,
        cpu_count=1,
        memory_mb=1024,
        on_build_logs=default_build_logger(),
    )
    print(f"E2B template ready: {settings.e2b_template} ({result.template_id})")


if __name__ == "__main__":
    asyncio.run(main())
