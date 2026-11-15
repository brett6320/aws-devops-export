# Import the tool first so truststore.inject_into_ssl() runs before urllib3 is
# imported (via boto3); otherwise SSLContext setup recurses.
import devops_export  # noqa: F401
