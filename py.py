#!/usr/bin/env python3
import argparse
import csv
import getpass
import json
import os
import subprocess
import tempfile
from decimal import Decimal


def cpu_cores(value):
    for suffix, factor in (
        ("n", Decimal("1e-9")),
        ("u", Decimal("1e-6")),
        ("m", Decimal("1e-3")),
    ):
        if value.endswith(suffix):
            return Decimal(value[:-1]) * factor
    return Decimal(value)


def memory_gib(value):
    units = {
        "Ki": 1024,
        "Mi": 1024**2,
        "Gi": 1024**3,
        "Ti": 1024**4,
        "Pi": 1024**5,
        "Ei": 1024**6,
        "k": 1000,
        "K": 1000,
        "M": 1000**2,
        "G": 1000**3,
        "T": 1000**4,
        "P": 1000**5,
        "E": 1000**6,
        "m": Decimal("1e-3"),
    }

    for suffix, factor in units.items():
        if value.endswith(suffix):
            return Decimal(value[:-len(suffix)]) * factor / 1024**3

    return Decimal(value) / 1024**3


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("clusters", help="Cluster listesinin TXT dosyası")
    parser.add_argument("-u", "--username")
    parser.add_argument("-o", "--output", default="cluster_resources.csv")
    parser.add_argument(
        "--insecure",
        action="store_true",
        help="TLS sertifika doğrulamasını kapat",
    )
    args = parser.parse_args()

    clusters = []

    with open(args.clusters, encoding="utf-8-sig") as source:
        for number, line in enumerate(source, 1):
            fields = line.split("#", 1)[0].split()

            if not fields:
                continue

            if len(fields) != 2 or not fields[1].startswith("https://"):
                parser.error(
                    f"{number}. satır hatalı. "
                    "Format: cluster_adi https://api.adres:6443"
                )

            clusters.append(fields)

    if not clusters:
        parser.error("Cluster listesi boş.")

    username = args.username or input("Kullanıcı adı: ")
    password = getpass.getpass("Şifre: ")
    failed = []

    # Aynı isimde CSV varsa üzerine yazar.
    with open(args.output, "w", newline="", encoding="utf-8-sig") as output:
        writer = csv.writer(output, delimiter=";")
        writer.writerow([
            "Cluster",
            "Node",
            "Rol",
            "CPU_Toplam_Core",
            "RAM_Toplam_GiB",
            "CPU_Allocatable_Core",
            "RAM_Allocatable_GiB",
        ])

        # Her cluster için ayrı geçici kubeconfig:
        # mevcut oc oturumuna dokunmaz.
        with tempfile.TemporaryDirectory(prefix="oc-resources-") as tmp:
            for index, (cluster, api) in enumerate(clusters):
                print(f"{cluster}: bağlanılıyor...", flush=True)

                env = dict(
                    os.environ,
                    KUBECONFIG=os.path.join(tmp, f"config-{index}"),
                )

                command = [
                    "oc", "login", api,
                    f"--username={username}",
                    f"--password={password}",
                    "--request-timeout=30s",
                ]

                if args.insecure:
                    command.append("--insecure-skip-tls-verify=true")

                try:
                    login = subprocess.run(
                        command,
                        env=env,
                        capture_output=True,
                        timeout=90,
                    )

                    if login.returncode:
                        print(f"{cluster}: giriş başarısız.")
                        failed.append(cluster)
                        continue

                    result = subprocess.run(
                        [
                            "oc", "get", "nodes",
                            "-o", "json",
                            "--request-timeout=30s",
                        ],
                        env=env,
                        capture_output=True,
                        timeout=60,
                    )

                    if result.returncode:
                        print(f"{cluster}: node bilgileri alınamadı.")
                        failed.append(cluster)
                        continue

                    records = []

                    for node in json.loads(result.stdout)["items"]:
                        labels = node["metadata"].get("labels", {})
                        roles = []

                        if any(
                            f"node-role.kubernetes.io/{role}" in labels
                            for role in ("master", "control-plane")
                        ):
                            roles.append("master")

                        if "node-role.kubernetes.io/infra" in labels:
                            roles.append("infra")

                        if not roles:
                            continue

                        capacity = node["status"]["capacity"]
                        allocatable = node["status"]["allocatable"]

                        records.append([
                            cluster,
                            node["metadata"]["name"],
                            "+".join(roles),
                            str(cpu_cores(capacity["cpu"])),
                            f'{memory_gib(capacity["memory"]):.2f}',
                            str(cpu_cores(allocatable["cpu"])),
                            f'{memory_gib(allocatable["memory"]):.2f}',
                        ])

                    writer.writerows(records)
                    output.flush()
                    print(f"{cluster}: {len(records)} node CSV'ye yazıldı.")

                except subprocess.TimeoutExpired:
                    print(f"{cluster}: zaman aşımı.")
                    failed.append(cluster)

                except FileNotFoundError:
                    print("HATA: oc komutu bulunamadı.")
                    return 1

                except (ValueError, KeyError) as error:
                    print(
                        f"{cluster}: veri işlenemedi "
                        f"({type(error).__name__})."
                    )
                    failed.append(cluster)

    print(f"\nCSV: {os.path.abspath(args.output)}")

    if failed:
        print("Başarısız clusterlar: " + ", ".join(failed))
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())