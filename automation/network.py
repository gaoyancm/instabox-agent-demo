"""Bounded, current-cycle egress observation; never claims a platform region guarantee."""
import ipaddress
import json
import subprocess
from .common import utcnow


def inspect_network():
    result = {"observed_at": utcnow().isoformat(), "platform_region": None,
              "machine_addresses": None, "egress_ipv4": None, "egress_ipv6": None,
              "country_code": None, "geo_region": None,
              "region_basis": "IP geolocation; not a guaranteed datacenter location",
              "sources": [], "errors": []}
    for family in [4, 6]:
        try:
            process = subprocess.run(["curl", f"-{family}", "--noproxy", "*", "--fail", "--silent",
                "--connect-timeout", "3", "--max-time", "6", "https://www.cloudflare.com/cdn-cgi/trace"],
                capture_output=True, timeout=8)
            fields = dict(line.split("=", 1) for line in process.stdout.decode("utf-8").splitlines() if "=" in line)
            address = ipaddress.ip_address(fields.get("ip", ""))
            if process.returncode or address.version != family or not address.is_global:
                raise ValueError("invalid_observation")
            result[f"egress_ipv{family}"] = str(address)
            result["sources"].append(f"https://www.cloudflare.com/cdn-cgi/trace (IPv{family})")
            if len(fields.get("loc", "")) == 2:
                result["country_code"] = fields["loc"]
        except (OSError, subprocess.SubprocessError, ValueError, UnicodeError):
            result["errors"].append(f"ipv{family}_unavailable")
    if result["egress_ipv4"]:
        try:
            url = "https://ipwho.is/" + result["egress_ipv4"]
            process = subprocess.run(["curl", "-4", "--noproxy", "*", "--fail", "--silent",
                "--connect-timeout", "3", "--max-time", "6", url], capture_output=True, timeout=8)
            geo = json.loads(process.stdout)
            if process.returncode or geo.get("success") is not True or geo.get("ip") != result["egress_ipv4"]:
                raise ValueError("unverified_geo")
            result["geo_region"] = " / ".join(str(geo[key])[:100] for key in ["country", "region", "city"] if geo.get(key))
            result["sources"].append(url)
        except (OSError, subprocess.SubprocessError, ValueError, TypeError):
            result["errors"].append("geolocation_unavailable")
    try:
        process = subprocess.run(["ip", "-j", "address", "show", "scope", "global"], capture_output=True, timeout=2)
        if process.returncode:
            raise ValueError("interface_query_failed")
        result["machine_addresses"] = sorted({str(ipaddress.ip_address(address["local"]))
            for interface in json.loads(process.stdout) for address in interface.get("addr_info", [])
            if address.get("family") in {"inet", "inet6"} and address.get("local")})
    except (OSError, subprocess.SubprocessError, ValueError, TypeError, KeyError):
        result["errors"].append("machine_addresses_unavailable")
    return result
