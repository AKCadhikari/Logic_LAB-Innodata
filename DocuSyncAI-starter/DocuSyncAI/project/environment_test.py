import sys
import fitz
import cv2
import lxml
import numpy
import pandas
import pptx
import streamlit

print("=" * 50)
print("DOCUSYNC AI - ENVIRONMENT DIAGNOSTICS")
print("=" * 50)

print(f"Python: {sys.version}")
print(f"PyMuPDF: {fitz.__doc__.split()[1]}")
print(f"OpenCV: {cv2.__version__}")
print(f"NumPy: {numpy.__version__}")
print(f"Pandas: {pandas.__version__}")

print("\nAll initial dependencies imported successfully.")
print("Environment ready for PDF intelligence development.")
