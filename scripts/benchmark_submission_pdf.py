"""Benchmark large submissions with synthetic photos and temporary local storage.

No environment settings, database, network, or participant data are used.
The subprocess timeout bounds a pathological PDF generation run.
"""
from __future__ import annotations

import argparse
import ast
import ctypes
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
from queue import Empty, Queue

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def peak_memory_bytes():
    if sys.platform == 'win32':
        from ctypes import wintypes

        class Counters(ctypes.Structure):
            _fields_ = [('cb', wintypes.DWORD), ('faults', wintypes.DWORD)] + [
                (name, ctypes.c_size_t) for name in (
                    'peak_working_set', 'working_set', 'peak_paged_pool', 'paged_pool',
                    'peak_nonpaged_pool', 'nonpaged_pool', 'pagefile', 'peak_pagefile')]

        counter = Counters()
        counter.cb = ctypes.sizeof(counter)
        current_process = ctypes.windll.kernel32.GetCurrentProcess
        current_process.restype = ctypes.c_void_p
        read_memory = ctypes.windll.psapi.GetProcessMemoryInfo
        read_memory.argtypes = [ctypes.c_void_p, ctypes.POINTER(Counters), wintypes.DWORD]
        if read_memory(current_process(), ctypes.byref(counter), counter.cb):
            return counter.peak_working_set
    else:
        import resource
        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * (1 if sys.platform == 'darwin' else 1024)
    return None


def run_worker(args):
    import fitz
    import numpy as np
    from PIL import Image, ImageDraw

    from app.main import make_pdf
    from app.storage import LocalStorage

    if args.compression_effort is not None:
        assert not args.baseline_ref, 'Compression override applies only to the native PDF implementation.'
        original_tobytes = fitz.Document.tobytes

        def tuned_tobytes(document, *positional, **keywords):
            keywords.setdefault('compression_effort', args.compression_effort)
            return original_tobytes(document, *positional, **keywords)

        fitz.Document.tobytes = tuned_tobytes

    if args.baseline_ref:
        from pypdf import PdfReader, PdfWriter
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.utils import ImageReader
        from reportlab.pdfgen import canvas
        saved_source = subprocess.check_output(
            ['git', '-c', f'safe.directory={ROOT.as_posix()}', 'show', f'{args.baseline_ref}:app/main.py'],
            cwd=ROOT, text=True, encoding='utf-8')
        definition = next(node for node in ast.parse(saved_source).body
                          if isinstance(node, ast.FunctionDef) and node.name == 'make_pdf')
        namespace = dict(io=io, PdfReader=PdfReader, PdfWriter=PdfWriter,
                         A4=A4, ImageReader=ImageReader, canvas=canvas)
        exec(compile(ast.Module(body=[definition], type_ignores=[]), '<baseline make_pdf>', 'exec'), namespace)
        make_pdf = namespace['make_pdf']

    assert 0 <= args.initial_photos < args.pages
    assert 0 < args.additional_photos <= args.pages - args.initial_photos
    source_path = ROOT / 'resources/worksheets/hiragana_collection_a4.pdf'
    source = source_path.read_bytes()
    with fitz.open(stream=source, filetype='pdf') as document:
        source_count = len(document)
    configured = [{'qr_code': f'BENCH-{index + 1:03d}', 'page_number': index + 1,
                   'file_page_number': index % source_count + 1, 'worksheet_id': 'benchmark-source'}
                  for index in range(args.pages)]
    participant = SimpleNamespace(participant_id='PBENCH', worksheet_revision_id='benchmark-source')

    class Registry:
        def pdf(self, worksheet_id):
            assert worksheet_id == 'benchmark-source'
            return source, source_path.name

    with tempfile.TemporaryDirectory(prefix='submission-benchmark-') as temporary:
        class RecordingStorage(LocalStorage):
            def __init__(self, root):
                super().__init__(root)
                self.reads = []

            def get(self, key):
                self.reads.append(key)
                if args.storage_latency:
                    time.sleep(args.storage_latency)
                return super().get(key)

        storage = RecordingStorage(Path(temporary))
        # Fine camera-like grain keeps PNGs distinct and avoids an unrealistically
        # tiny all-white fixture. Every image is at the application's output size.
        rng = np.random.default_rng(20261119)
        grain = np.clip(rng.normal(246, 2.5, (2339, 1654)), 0, 255).astype(np.uint8)
        base = Image.fromarray(grain)
        pages, bytes_total = [], 0
        for index in range(args.initial_photos + args.additional_photos):
            photo = base.copy()
            draw = ImageDraw.Draw(photo)
            draw.text((150, 150), f'SYNTHETIC BENCHMARK PHOTO {index + 1}', fill=20,
                      font_size=40)
            for y in range(250, 2000, 80):
                draw.line((150, y, 1450, y + index % 5), fill=110, width=2)
            output = io.BytesIO()
            photo.save(output, format='PNG', compress_level=1)
            data, key = output.getvalue(), f'corrected-{index + 1:03d}.png'
            storage.put(key, data, 'image/png')
            bytes_total += len(data)
            pages.append(SimpleNamespace(qr_code=configured[index]['qr_code'],
                                         page_number=index + 1, corrected_path=key))
        print(json.dumps({'stage': 'fixtures', 'photos': len(pages), 'png_bytes': bytes_total}), flush=True)

        started = time.perf_counter()
        initial = make_pdf(participant, pages[:args.initial_photos], storage, configured, Registry())
        initial_seconds = time.perf_counter() - started
        print(json.dumps({'stage': 'initial', 'seconds': initial_seconds, 'bytes': len(initial),
                          'peak_memory_bytes': peak_memory_bytes()}), flush=True)
        storage.reads.clear()
        added = pages[args.initial_photos:]
        started = time.perf_counter()
        updated = make_pdf(participant, pages, storage, configured, Registry(), previous_pdf=initial,
                           updated_qrs={page.qr_code for page in added})
        update_seconds = time.perf_counter() - started
        update_reads = list(storage.reads)
        print(json.dumps({'stage': 'additional', 'seconds': update_seconds, 'bytes': len(updated),
                          'storage_reads': len(update_reads), 'peak_memory_bytes': peak_memory_bytes()}), flush=True)

        assert sorted(update_reads) == sorted(page.corrected_path for page in added), update_reads
        with fitz.open(stream=initial, filetype='pdf') as before, fitz.open(stream=updated, filetype='pdf') as after:
            assert len(before) == len(after) == args.pages
            pixels = lambda document, index: document[index].get_pixmap(matrix=fitz.Matrix(.5, .5)).samples
            unchanged_indexes = {0, args.initial_photos - 1, args.pages - 1}
            unchanged_indexes -= set(range(args.initial_photos, len(pages)))
            for index in unchanged_indexes:
                if index >= 0:
                    assert pixels(before, index) == pixels(after, index), index
            changed_indexes = {args.initial_photos, len(pages) - 1}
            for index in changed_indexes:
                assert pixels(before, index) != pixels(after, index), index
            # Inspect the actual embedded pixels, not only a completed status.
            for index in {0, args.initial_photos - 1, args.initial_photos, len(pages) - 1}:
                if index < 0:
                    continue
                images = after[index].get_images(full=True)
                assert len(images) == 1, (index, len(images))
                embedded = Image.open(io.BytesIO(after.extract_image(images[0][0])['image'])).convert('RGB')
                expected = Image.open(io.BytesIO(storage.path(pages[index].corrected_path).read_bytes())).convert('RGB')
                assert embedded.size == expected.size and embedded.tobytes() == expected.tobytes(), index
            assert len(after.get_toc()) == args.pages
        return {'ok': True, 'pages': args.pages, 'initial_photos': args.initial_photos,
                'additional_photos': args.additional_photos, 'initial_seconds': initial_seconds,
                'additional_seconds': update_seconds, 'initial_pdf_bytes': len(initial),
                'updated_pdf_bytes': len(updated), 'corrected_photo_bytes': bytes_total,
                'peak_memory_bytes': peak_memory_bytes(), 'additional_storage_reads': len(update_reads),
                'old_photos_reread': False, 'unchanged_pages_match': True, 'new_photos_match': True,
                'storage_latency_seconds': args.storage_latency, 'synthetic_local_data_only': True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pages', type=int, default=128)
    parser.add_argument('--initial-photos', type=int, default=14)
    parser.add_argument('--additional-photos', type=int, default=15)
    parser.add_argument('--storage-latency', type=float, default=0)
    parser.add_argument('--compression-effort', type=int, choices=range(101), help='Profile native PDF compression without changing app code (0=default, 1=minimum, 100=maximum).')
    parser.add_argument('--timeout', type=float, default=60)
    parser.add_argument('--baseline-ref', help='Read the previous make_pdf implementation from this local Git revision.')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--worker', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker:
        print(json.dumps(run_worker(args)), flush=True)
        return
    command = [sys.executable, str(Path(__file__).resolve()), '--worker', '--pages', str(args.pages),
               '--initial-photos', str(args.initial_photos), '--additional-photos', str(args.additional_photos),
               '--storage-latency', str(args.storage_latency)]
    if args.baseline_ref:
        command.extend(['--baseline-ref', args.baseline_ref])
    if args.compression_effort is not None:
        command.extend(['--compression-effort', str(args.compression_effort)])
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               text=True, bufsize=1)
    queue = Queue()

    def read_output():
        for line in process.stdout:
            queue.put(line)
        queue.put(None)

    threading.Thread(target=read_output, daemon=True).start()
    lines = []
    while True:
        try:
            line = queue.get(timeout=args.timeout)
        except Empty:
            process.kill()
            process.wait(timeout=10)
            raise SystemExit(f'Benchmark stage exceeded {args.timeout:g}s; worker stopped.') from None
        if line is None:
            break
        lines.append(line)
        print(line, end='', flush=True)
    if process.wait(timeout=10):
        raise SystemExit(process.returncode)
    if args.output:
        report = json.loads(lines[-1])
        report['implementation'] = args.baseline_ref or 'workspace'
        report['compression_effort_override'] = args.compression_effort
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')


if __name__ == '__main__':
    main()
