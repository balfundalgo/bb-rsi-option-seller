import subprocess, sys
def build():
    cmd = [sys.executable, "-m", "PyInstaller", "--onefile", "--windowed",
           "--name", "BB-RSI-Option-Seller", "--icon", "NONE",
           "--add-data", ".env;.",
           "--hidden-import", "customtkinter", "--collect-all", "customtkinter",
           "bb_rsi_seller.py"]
    subprocess.run(cmd, check=True)
    print("\n✓ Build complete! EXE in dist/")
if __name__ == "__main__": build()
