import truststore

# Before anything opens a connection: see windows/requirements.txt for why
truststore.inject_into_ssl()

from kaho_spike.app import main  # noqa: E402

main()
