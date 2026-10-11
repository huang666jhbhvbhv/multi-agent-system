"""Native stderr fixture; optional argument sets the real process exit code."""
import sys

sys.stderr.write("normal diagnostic output\n")
sys.exit(int(sys.argv[1]) if len(sys.argv) > 1 else 0)
