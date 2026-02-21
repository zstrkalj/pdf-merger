"""Entry point for the PDF Merger application."""

import sys
from PyQt6.QtWidgets import QApplication
from pdf_merger.ui.main_window import MainWindow


def main():
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
