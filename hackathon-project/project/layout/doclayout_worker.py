"""JSON-lines inference in the isolated DocLayout-YOLO environment."""
import json
import os
from pathlib import Path
import sys


def main():
    protocol = sys.stdout
    sys.stdout = sys.stderr
    repo, checkpoint = map(Path, sys.argv[1:3])

    def send(message):
        print(json.dumps(message, allow_nan=False), file=protocol, flush=True)

    try:
        sys.path.insert(0, str(repo))
        import torch
        from ultralytics import YOLO

        torch.set_num_threads(min(os.cpu_count() or 1, 4))
        model = YOLO(str(checkpoint)).to("cpu")
        names = {int(key): value for key, value in model.names.items()}
        send({"status": "ready", "device": str(next(model.model.parameters()).device), "classes": names})
    except Exception as error:
        send({"error": str(error)})
        return

    for line in sys.stdin:
        request = {}
        try:
            request = json.loads(line)
            result, = model.predict(request["image"], imgsz=request["size"], conf=request["confidence"],
                                    device="cpu", verbose=False, save=False)
            boxes = result.boxes
            regions = [
                {"class_id": int(label), "class_name": names[int(label)], "confidence": score, "pixels": box}
                for label, score, box in zip(boxes.cls.tolist(), boxes.conf.tolist(), boxes.xyxy.tolist())
            ]
            send({"page": request["page"], "shape": result.orig_shape, "regions": regions})
        except Exception as error:
            send({"page": request.get("page"), "error": str(error)})


if __name__ == "__main__":
    main()
