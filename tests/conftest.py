import pytest

from app.core.config import Settings

TEST_TOKEN = "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi"


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    for key in (
        "BOT_TOKEN",
        "BOOTSTRAP_ADMIN_IDS",
        "APP_TIMEZONE",
        "DATA_DIR",
        "SECRETS_DIR",
        "RETENTION_ENABLED",
    ):
        monkeypatch.delenv(key, raising=False)


@pytest.fixture
def settings():
    return Settings(
        _env_file=None,
        bot_token=TEST_TOKEN,
        bootstrap_admin_ids=[111],
    )


@pytest.fixture
async def identity_service(settings, tmp_path):
    from app.core.security import create_keyring
    from app.database.storage import Database, prepare_storage
    from app.services.identity_service import IdentityService

    configured = settings.model_copy(
        update={"data_dir": tmp_path / "data", "secrets_dir": tmp_path / "keys"}
    )
    create_keyring(configured.secrets_dir / "keyring.json")
    await prepare_storage(configured)
    database = Database(configured.data_dir / "identity.db", "identity")
    service = IdentityService(database)
    await service.bootstrap((111,))
    try:
        yield service
    finally:
        await database.close()


@pytest.fixture
async def feedback_service(identity_service, tmp_path):
    from app.core.security import Keyring, Scope, ScopeCipher
    from app.database.storage import Database
    from app.services.feedback_service import FeedbackService

    database = Database(tmp_path / "data" / "internal.db", "internal")
    cipher = ScopeCipher(Keyring.load(tmp_path / "keys" / "keyring.json"), Scope.INTERNAL)
    feedback = FeedbackService(identity_service, database, cipher)
    await identity_service.authenticate(111, started=True)
    await identity_service.add_user(111, 222, "همکار گیرنده")
    await identity_service.add_user(111, 333, "همکار فرستنده")
    await identity_service.authenticate(222, started=True)
    await identity_service.authenticate(333, started=True)
    try:
        yield feedback
    finally:
        await database.close()
