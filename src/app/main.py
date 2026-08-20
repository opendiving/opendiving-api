from .admin.initialize import create_admin_interface
from .api import router
from .core.config import settings
from .core.setup import create_application, lifespan_factory

admin = create_admin_interface()

# The admin panel's *schema* setup (`admin.initialize()`) deliberately does not happen
# here. It used to run inside a custom lifespan, which meant every gunicorn worker did it:
# with `-w 4` the four workers raced to create the admin tables and to insert the initial
# admin row, and whichever ones lost died with `table admin_user already exists` or
# `UNIQUE constraint failed: admin_user.username` - taking the whole container down.
#
# It is a one-shot schema-and-seed step, not per-process state, so it lives in
# `admin.initialize`'s `main()` and runs once before the API starts (see the `admin_init`
# service in `docker-compose.yml`). Constructing the interface above still registers all
# its routes, so mounting works in every worker without any of them touching the DB.
app = create_application(router=router, settings=settings, lifespan=lifespan_factory(settings))

# Mount admin interface if enabled
if admin:
    app.mount(settings.CRUD_ADMIN_MOUNT_PATH, admin.app)
