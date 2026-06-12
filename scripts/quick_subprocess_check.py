import subprocess
# cmd notes
# w32tm /resync
# sc.exe query w32time
# sc.exe start w32time
# sc.exe stop w32time
if __name__ == "__main__":
    status = subprocess.run(["w32tm", "/resync"], capture_output=True, text=True)
    print(status.args, type(status.args))
    print(status.returncode, type(status.returncode))
    print(f"'{status.stderr}'")
    print(f"'{status.stdout}'")
