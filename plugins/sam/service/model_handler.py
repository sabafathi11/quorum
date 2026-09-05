# Copyright (C) CVAT.ai Corporation
#
# SPDX-License-Identifier: MIT

import numpy as np
import torch
import cv2
import collections
import uuid
import os
import time
import json

# ---------------------------------------------------------------------------
# Mask encoding helpers
# ---------------------------------------------------------------------------

def mask_to_rle(mask):
    """Convert a binary mask to the CVAT RLE format (runs + bounding box)."""
    [height, width] = mask.shape
    rows = np.any(mask, axis=1)
    cols = np.any(mask, axis=0)
    if not np.any(rows):
        return []
    ymin, ymax = np.where(rows)[0][[0, -1]]
    xmin, xmax = np.where(cols)[0][[0, -1]]
    cropped_mask = mask[ymin:ymax+1, xmin:xmax+1]
    pixels = (np.asarray(cropped_mask).reshape(-1) != 0).astype(np.uint8)
    if pixels.size == 0:
        return []
    changes = np.flatnonzero(pixels[1:] != pixels[:-1]) + 1
    rle = np.diff(np.concatenate(([0], changes, [pixels.size]))).tolist()
    if pixels[0] == 1:
        rle.insert(0, 0)
    rle.extend([int(xmin), int(ymin), int(xmax), int(ymax)])
    return rle


def encode_mask(mask: np.ndarray) -> list[float]:
    """Convert a binary mask to a polygon (largest contour)."""
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return []
    largest_contour = max(contours, key=cv2.contourArea)
    approx_contour = cv2.approxPolyDP(largest_contour, epsilon=1.0, closed=True)
    if approx_contour.shape[0] < 3:
        return []
    return approx_contour.flatten().tolist()


def decode_mask_shape(shape, width, height):
    """Decode a shape dict (polygon/rectangle/mask) into a binary numpy mask."""
    if shape["type"] == "polygon":
        mask = np.zeros((height, width), dtype=np.uint8)
        points_array = np.array(shape["points"], dtype=np.int32).reshape((-1, 2))
        cv2.fillPoly(mask, [points_array], 1)
        return mask
    elif shape["type"] == "rectangle":
        mask = np.zeros((height, width), dtype=np.uint8)
        p = shape["points"]
        cv2.rectangle(mask, (int(p[0]), int(p[1])), (int(p[2]), int(p[3])), 1, -1)
        return mask
    elif shape["type"] == "mask":
        rle = shape["points"]
        left, top, right, bottom = rle[-4:]
        runs = rle[:-4]
        box_width = int(right - left + 1)
        box_height = int(bottom - top + 1)
        box_pixels = np.zeros(box_width * box_height, dtype=np.uint8)
        offset = 0
        val = 0
        for run in runs:
            box_pixels[offset:offset+int(run)] = val
            offset += int(run)
            val = 1 - val
        box_mask = box_pixels.reshape((box_height, box_width))
        full_mask = np.zeros((height, width), dtype=np.uint8)
        full_mask[int(top):int(bottom)+1, int(left):int(right)+1] = box_mask
        return full_mask
    return np.zeros((height, width), dtype=np.uint8)


# ---------------------------------------------------------------------------
# State persistence (file-based for stateful tracking)
# ---------------------------------------------------------------------------

def encode_state(state_dict):
    state_id = str(uuid.uuid4())
    state_dir = "/tmp/sam3_states"
    os.makedirs(state_dir, exist_ok=True)
    now = time.time()
    for filename in os.listdir(state_dir):
        filepath = os.path.join(state_dir, filename)
        if os.path.isfile(filepath) and now - os.path.getmtime(filepath) > 3600:
            try:
                os.remove(filepath)
            except Exception:
                pass
    with open(f"{state_dir}/{state_id}.pt", "wb") as f:
        torch.save(state_dict, f)
    return state_id


def decode_state(state_str):
    try:
        with open(f"/tmp/sam3_states/{state_str}.pt", "rb") as f:
            return torch.load(f, map_location="cpu", weights_only=False)
    except FileNotFoundError:
        return None


# ---------------------------------------------------------------------------
# Model handler
# ---------------------------------------------------------------------------

class ModelHandler:
    def __init__(self):
        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        self.checkpoint_path = "/opt/nuclio/sam/sam3.1_multiplex_fp16.safetensors"

        if self.device.type == "cuda":
            torch.set_autocast_enabled(True)
            torch.set_autocast_gpu_dtype(torch.bfloat16)

        # Build the SAM3.1 Multiplex tracker with its own backbone
        from sam3.model_builder import build_sam3_multiplex_video_model

        # --- MONKEY PATCH FOR TriHeadVisionOnly ---
        from sam3.model.vl_combiner import TriHeadVisionOnly
        orig_forward_image = TriHeadVisionOnly.forward_image

        def patched_forward_image(self, *args, **kwargs):
            out = orig_forward_image(self, *args, **kwargs)
            if "vision_features" in out:
                out["sam3_out"] = {
                    "vision_features": out.pop("vision_features"),
                    "vision_mask": out.pop("vision_mask"),
                    "vision_pos_enc": out.pop("vision_pos_enc"),
                    "backbone_fpn": out.pop("backbone_fpn"),
                }
            return out

        TriHeadVisionOnly.forward_image = patched_forward_image
        # ------------------------------------------

        self.video_predictor = build_sam3_multiplex_video_model(
            checkpoint_path=None,
            load_from_HF=False,
            strict_state_dict_loading=False,
        )

        # Load the ComfyUI safetensors checkpoint and remap keys
        from safetensors.torch import load_file
        ckpt = load_file(self.checkpoint_path)

        # The checkpoint has two top-level prefixes:
        #   tracker.model.*  -> goes into the tracker (self.video_predictor)
        #   detector.backbone.* -> the shared backbone; the tracker's backbone
        #       uses the SAM3VLBackbone structure with .visual (the ViTDetNeck)
        # The tracker built with with_backbone=True has:
        #   self.backbone = SAM3VLBackbone(visual=vit_neck, text=None)
        # The detector checkpoint has:
        #   detector.backbone.visual.* -> ViTDetNeck weights
        #   detector.backbone.language_backbone.* -> text encoder (we skip this)
        mapped_ckpt = {}
        for k, v in ckpt.items():
            if k.startswith("tracker.model."):
                new_k = k[len("tracker.model."):]
                mapped_ckpt[new_k] = v.float()
            elif k.startswith("detector.backbone.vision_backbone."):
                # Map detector backbone visual -> tracker backbone visual
                new_k = "backbone.vision_backbone." + k[len("detector.backbone.vision_backbone."):]
                mapped_ckpt[new_k] = v.float()

        missing, unexpected = self.video_predictor.load_state_dict(mapped_ckpt, strict=False)
        # Filter out expected missing keys (text encoder, detector-only stuff)
        real_missing = [k for k in missing if not k.startswith("backbone.text.")]
        print(f"Tracker load: {len(real_missing)} truly missing, {len(unexpected)} unexpected keys")
        if real_missing:
            print(f"  Missing (first 10): {real_missing[:10]}")
        if unexpected:
            print(f"  Unexpected (first 10): {unexpected[:10]}")

        self.video_predictor.to(device=self.device)
        self.video_predictor.eval()

        # Build the image predictor for interactor (click/box -> mask)
        from sam3.model.sam1_task_predictor import SAM3InteractiveImagePredictor

        class InteractiveModelWrapper:
            def __init__(self, model):
                self.model = model
                self._cached_prompt_encoder = None
                self._cached_mask_decoder = None

            def forward_image(self, input_image):
                return self.model.forward_image(input_image, need_interactive_out=True)

            def _prepare_backbone_features(self, backbone_out):
                feats = self.model._prepare_backbone_features(backbone_out)
                inter = feats["interactive"]
                return None, inter["vision_feats"], None, None

            @property
            def image_size(self):
                return self.model.image_size

            @property
            def no_mem_embed(self):
                # Fallback to 0.0 broadcast scalar for models lacking this param
                if hasattr(self.model, "no_mem_embed"):
                    return self.model.no_mem_embed
                return 0.0

            @property
            def sam_prompt_encoder(self):
                if self._cached_prompt_encoder is not None:
                    return self._cached_prompt_encoder

                # Dynamically search the module tree for the prompt encoder
                modules = dict(self.model.named_modules())
                # ADDED: 'interactive_sam_prompt_encoder' to match SAM 3.1 Multiplex internals
                for target in ["interactive_sam_prompt_encoder", "sam_prompt_encoder", "prompt_encoder"]:
                    for name, module in modules.items():
                        if name == target or name.endswith("." + target):
                            self._cached_prompt_encoder = module
                            return module
                raise AttributeError("Could not find prompt encoder in model tree")

            @property
            def sam_mask_decoder(self):
                if self._cached_mask_decoder is not None:
                    return self._cached_mask_decoder

                # Dynamically search prioritizing the interactive mask decoder
                modules = dict(self.model.named_modules())
                for target in ["interactive_sam_mask_decoder", "sam_mask_decoder", "mask_decoder"]:
                    for name, module in modules.items():
                        if name == target or name.endswith("." + target):
                            self._cached_mask_decoder = module
                            return module
                raise AttributeError("Could not find mask decoder in model tree")

            @property
            def device(self):
                return self.model.device

        self.video_predictor.device = self.device
        self.image_predictor = SAM3InteractiveImagePredictor(InteractiveModelWrapper(self.video_predictor))

        # Image preprocessing transform for tracker
        import torchvision.transforms
        self.transform = torchvision.transforms.Compose([
            torchvision.transforms.Resize(
                (self.video_predictor.image_size, self.video_predictor.image_size)
            ),
            torchvision.transforms.ToTensor(),
            torchvision.transforms.Normalize(
                mean=(0.485, 0.456, 0.406),
                std=(0.229, 0.224, 0.225),
            ),
        ])

    # -------------------------------------------------------------------
    # Interactor: click/box -> mask
    # -------------------------------------------------------------------
    def handle_interact(self, image, pos_points, neg_points, obj_bbox):
        self.image_predictor.set_image(np.array(image))

        points = []
        labels = []
        for p in pos_points:
            points.append(p)
            labels.append(1)
        for p in neg_points:
            points.append(p)
            labels.append(0)

        points = np.array(points, dtype=np.float32) if points else None
        labels = np.array(labels, dtype=np.int32) if labels else None

        box = None
        if obj_bbox:
            box = np.array(obj_bbox, dtype=np.float32)
            if len(box.shape) > 1:
                box = box.reshape(-1)

        if points is None and box is None:
            return []

        masks, scores, logits = self.image_predictor.predict(
            point_coords=points,
            point_labels=labels,
            box=box,
            multimask_output=False,
        )
        mask = masks[0]
        return mask_to_rle(mask)

    # -------------------------------------------------------------------
    # Tracker helpers
    # -------------------------------------------------------------------
    def _preprocess_image(self, image):
        image_tensor = self.transform(image).unsqueeze(0).to(device=self.device)
        with torch.inference_mode():
            backbone_out = self.video_predictor.forward_image(image_tensor)
            _, vision_feats, vision_pos_embeds, feat_sizes = \
                self.video_predictor._prepare_backbone_features(backbone_out)

        return {
            "original_width": image.width,
            "original_height": image.height,
            "vision_feats": vision_feats,
            "vision_pos_embeds": vision_pos_embeds,
            "feat_sizes": feat_sizes,
        }

    def _call_predictor(self, pp_image, frame_idx, mask_inputs=None,
                        output_dict=None, is_init_cond_frame=False):
        if output_dict is None:
            output_dict = {}
        with torch.inference_mode():
            out = self.video_predictor.track_step(
                frame_idx=frame_idx,
                is_init_cond_frame=is_init_cond_frame,
                current_vision_feats=pp_image["vision_feats"],
                current_vision_pos_embeds=pp_image["vision_pos_embeds"],
                feat_sizes=pp_image["feat_sizes"],
                image=None,
                point_inputs=None,
                mask_inputs=mask_inputs,
                output_dict=output_dict,
                num_frames=frame_idx + 1,
            )
            pred_masks = out["pred_masks"]
            if hasattr(self.video_predictor, 'fill_hole_area') and self.video_predictor.fill_hole_area > 0:
                try:
                    from sam3.model.sam3_tracker_utils import fill_holes_in_mask_scores
                    pred_masks = fill_holes_in_mask_scores(
                        pred_masks, self.video_predictor.fill_hole_area
                    )
                except ImportError:
                    pass

            return {
                "maskmem_features": out["maskmem_features"],
                "maskmem_pos_enc": out["maskmem_pos_enc"][-1:],
                "pred_masks": pred_masks,
                "obj_ptr": out["obj_ptr"],
            }

    # -------------------------------------------------------------------
    # Tracker: Batch processing for SAM 3.1 Multiplex
    # -------------------------------------------------------------------
    def handle_track_batch(self, images, shapes, states):
        import tempfile
        import shutil
        import os
        import uuid
        import json

        # Ensure tmp dir exists
        os.makedirs("/tmp/sam3_shapes", exist_ok=True)

        inference_state = self.video_predictor.init_state(
            video_height=images[0].height,
            video_width=images[0].width,
            num_frames=len(images),
            offload_video_to_cpu=False,
            offload_state_to_cpu=False
        )

        # Manually construct inference_state["images"] since init_state doesn't
        tensor_images = []
        for img in images:
            t = self.transform(img)
            tensor_images.append(t.to(self.device))
        inference_state["images"] = tensor_images

        masks_to_add = []
        obj_ids = []
        for i, (shape, state_str) in enumerate(zip(shapes, states)):
            current_shape = shape
            if current_shape is None and state_str is not None:
                # Try to load shape from state file
                try:
                    with open(f"/tmp/sam3_shapes/{state_str}.json", "r") as f:
                        current_shape = json.load(f)
                except Exception as e:
                    print(f"Failed to load state shape: {e}")

            if current_shape is not None:
                mask = decode_mask_shape(current_shape, images[0].width, images[0].height)
                masks_to_add.append(torch.from_numpy(mask).float())
                obj_ids.append(i)

        if masks_to_add:
            masks_tensor = torch.stack(masks_to_add)
            self.video_predictor.add_new_masks(
                inference_state,
                frame_idx=0,
                obj_ids=obj_ids,
                masks=masks_tensor,
            )

        # FIX: Explicitly run the preflight to encode the initial masks into the memory bank!
        # Without this, frame 1 receives no memory and outputs random noise ("static").
        self.video_predictor.propagate_in_video_preflight(inference_state)

        results_shapes = [[] for _ in shapes]
        final_masks = [None] * len(shapes)

        # Propagate through the batch
        for out_frame_idx, out_obj_ids, out_low_res_masks, out_mask_logits, out_obj_scores in self.video_predictor.propagate_in_video(
            inference_state,
            start_frame_idx=0,
            max_frame_num_to_track=len(images),
            reverse=False
        ):
            pred_masks = (out_mask_logits > 0.0).squeeze(1).cpu().numpy().astype(np.uint8)
            for j, obj_id in enumerate(out_obj_ids):
                rle = mask_to_rle(pred_masks[j])
                results_shapes[obj_id].append({"type": "mask", "points": rle})
                final_masks[obj_id] = pred_masks[j]

        # Generate states for the next batch
        new_states = []
        for m in final_masks:
            if m is not None:
                state_id = str(uuid.uuid4())
                shape_dict = {"type": "mask", "points": mask_to_rle(m)}
                with open(f"/tmp/sam3_shapes/{state_id}.json", "w") as f:
                    json.dump(shape_dict, f)
                new_states.append(state_id)
            else:
                new_states.append(None)

        return results_shapes, new_states
