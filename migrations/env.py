from alembic import context
from sqlalchemy import URL, create_engine

from video_agent.db import validate_mysql_version
from video_agent.db_schema import metadata
from video_agent.runtime import RuntimeSettings

config = context.config


def database_url():
    settings = RuntimeSettings.from_env(validate_tokens=False)
    return URL.create("mysql+pymysql", username=settings.db_user, password=settings.db_password,
                      host=settings.db_host, port=settings.db_port, database=settings.db_name,
                      query={"charset": "utf8mb4"})


def run_migrations(connection):
    context.configure(connection=connection, target_metadata=metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    context.configure(url=database_url(), target_metadata=metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    supplied = config.attributes.get("connection")
    if supplied is not None:
        run_migrations(supplied)
    else:
        engine = create_engine(database_url(), hide_parameters=True)
        try:
            with engine.connect() as connection:
                version = str(connection.exec_driver_sql("SELECT VERSION()").scalar_one())
                validate_mysql_version(version)
                connection.commit()
                run_migrations(connection)
        finally:
            engine.dispose()
