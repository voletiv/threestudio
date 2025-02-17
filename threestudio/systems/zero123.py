import os
import random
import shutil
from dataclasses import dataclass, field

import torch
import torch.nn.functional as F

import threestudio
from threestudio.systems.base import BaseLift3DSystem
from threestudio.utils.ops import binary_cross_entropy, dot
from threestudio.utils.typing import *


@threestudio.register("zero123-system")
class Zero123(BaseLift3DSystem):
    @dataclass
    class Config(BaseLift3DSystem.Config):
        freq: dict = field(default_factory=dict)

    cfg: Config

    def configure(self):
        # create geometry, material, background, renderer
        super().configure()

    def forward(self, batch: Dict[str, Any]) -> Dict[str, Any]:
        render_out = self.renderer(**batch)
        return {
            **render_out,
        }

    def on_fit_start(self) -> None:
        super().on_fit_start()
        # no prompt processor
        self.guidance = threestudio.find(self.cfg.guidance_type)(self.cfg.guidance)

        # visualize all training images
        all_images = self.trainer.datamodule.train_dataloader().dataset.get_all_images()
        self.save_image_grid(
            "all_training_images.png",
            [
                {"type": "rgb", "img": image, "kwargs": {"data_format": "HWC"}}
                for image in all_images
            ],
        )

    def training_step(self, batch, batch_idx):
        # opt = self.optimizers()
        # opt.zero_grad()

        do_ref = (
            self.true_global_step < self.cfg.freq.ref_only_steps
            or self.true_global_step % self.cfg.freq.n_ref == 0
        )
        loss = 0.0

        if do_ref:
            # bg_color = torch.rand_like(batch['rays_o'])
            ambient_ratio = 1.0
            shading = "diffuse"
            batch["shading"] = shading
            bg_color = None
        else:
            batch = batch["random_camera"]
            if random.random() > 0.5:
                bg_color = None
            else:
                bg_color = torch.rand(3).to(self.device)
            ambient_ratio = 0.1 + 0.9 * random.random()

        batch["bg_color"] = bg_color
        batch["ambient_ratio"] = ambient_ratio

        out = self(batch)

        if do_ref:
            gt_mask = batch["mask"]
            gt_rgb = batch["rgb"]
            gt_depth = batch["depth"]

            guidance_out = {}

            # color loss
            gt_rgb = gt_rgb * gt_mask.float() + out["comp_rgb_bg"] * (
                1 - gt_mask.float()
            )
            guidance_out["loss_rgb"] = F.mse_loss(gt_rgb, out["comp_rgb"])

            # mask loss
            guidance_out["loss_mask"] = F.mse_loss(gt_mask.float(), out["opacity"])

            # depth loss
            if self.C(self.cfg.loss.lambda_depth) > 0:
                valid_gt_depth = gt_depth[gt_mask.squeeze(-1)].unsqueeze(1)
                valid_pred_depth = out["depth"][gt_mask].unsqueeze(1)
                with torch.no_grad():
                    A = torch.cat(
                        [valid_gt_depth, torch.ones_like(valid_gt_depth)], dim=-1
                    )  # [B, 2]
                    X = torch.linalg.lstsq(A, valid_pred_depth).solution  # [2, 1]
                    valid_gt_depth = A @ X  # [B, 1]
                guidance_out["loss_depth"] = F.mse_loss(
                    valid_gt_depth, valid_pred_depth
                )
        else:
            guidance_out = self.guidance(out["comp_rgb"], **batch, rgb_as_latents=False)

        loss = 0.0
        for name, value in guidance_out.items():
            self.log(f"train/{name}", value)
            if name.startswith("loss_"):
                loss += value * self.C(self.cfg.loss[name.replace("loss_", "lambda_")])

        if self.C(self.cfg.loss.lambda_orient) > 0:
            if "normal" not in out:
                raise ValueError(
                    "Normal is required for orientation loss, no normal is found in the output."
                )
            loss_orient = (
                out["weights"].detach()
                * dot(out["normal"], out["t_dirs"]).clamp_min(0.0) ** 2
            ).sum() / (out["opacity"] > 0).sum()
            self.log("train/loss_orient", loss_orient)
            loss += loss_orient * self.C(self.cfg.loss.lambda_orient)

        if self.C(self.cfg.loss.lambda_normal_smooth) > 0:
            if "comp_normal" not in out:
                raise ValueError(
                    "comp_normal is required for 2D normal smooth loss, no comp_normal is found in the output."
                )
            normal = out["comp_normal"]
            loss_normal_smooth = (
                normal[:, 1:, :, :] - normal[:, :-1, :, :]
            ).square().mean() + (
                normal[:, :, 1:, :] - normal[:, :, :-1, :]
            ).square().mean()
            self.log("train/loss_normal_smooth", loss_normal_smooth)
            loss += self.C(self.cfg.loss.lambda_normal_smooth) * loss_normal_smooth

        if self.C(self.cfg.loss.lambda_3d_normal_smooth) > 0:
            if "normal" not in out:
                raise ValueError(
                    "Normal is required for normal smooth loss, no normal is found in the output."
                )
            if "normal_perturb" not in out:
                raise ValueError(
                    "normal_perturb is required for normal smooth loss, no normal_perturb is found in the output."
                )
            normals = out["normal"]
            normals_perturb = out["normal_perturb"]
            loss_3d_normal_smooth = (normals - normals_perturb).abs().mean()
            self.log("train/loss_3d_normal_smooth", loss_3d_normal_smooth)
            loss += (
                self.C(self.cfg.loss.lambda_3d_normal_smooth) * loss_3d_normal_smooth
            )

        loss_sparsity = (out["opacity"] ** 2 + 0.01).sqrt().mean()
        self.log("train/loss_sparsity", loss_sparsity)
        loss += loss_sparsity * self.C(self.cfg.loss.lambda_sparsity)

        opacity_clamped = out["opacity"].clamp(1.0e-3, 1.0 - 1.0e-3)
        loss_opaque = binary_cross_entropy(opacity_clamped, opacity_clamped)
        self.log("train/loss_opaque", loss_opaque)
        loss += loss_opaque * self.C(self.cfg.loss.lambda_opaque)

        for name, value in self.cfg.loss.items():
            self.log(f"train_params/{name}", self.C(value))

        self.log("train/loss", loss, prog_bar=True)

        return {"loss": loss}

    def validation_step(self, batch, batch_idx):
        out = self(batch)
        self.save_image_grid(
            f"it{self.true_global_step}-val/{batch['index'][0]}.png",
            (
                [
                    {
                        "type": "rgb",
                        "img": batch["rgb"][0],
                        "kwargs": {"data_format": "HWC"},
                    }
                ]
                if "rgb" in batch
                else []
            )
            + [
                {
                    "type": "rgb",
                    "img": out["comp_rgb"][0],
                    "kwargs": {"data_format": "HWC"},
                },
            ]
            + (
                [
                    {
                        "type": "rgb",
                        "img": out["comp_normal"][0],
                        "kwargs": {"data_format": "HWC", "data_range": (0, 1)},
                    }
                ]
                if "comp_normal" in out
                else []
            )
            + [{"type": "grayscale", "img": out["depth"][0], "kwargs": {}}]
            + [
                {
                    "type": "grayscale",
                    "img": out["opacity"][0, :, :, 0],
                    "kwargs": {"cmap": None, "data_range": (0, 1)},
                },
            ],
        )

    def on_validation_epoch_end(self):
        self.save_img_sequence(
            f"it{self.true_global_step}-val",
            f"it{self.true_global_step}-val",
            "(\d+)\.png",
            save_format="mp4",
            fps=30,
        )
        shutil.rmtree(
            os.path.join(self.get_save_dir(), f"it{self.true_global_step}-val")
        )

    def test_step(self, batch, batch_idx):
        out = self(batch)
        self.save_image_grid(
            f"it{self.true_global_step}-test/{batch['index'][0]}.png",
            (
                [
                    {
                        "type": "rgb",
                        "img": batch["rgb"][0],
                        "kwargs": {"data_format": "HWC"},
                    }
                ]
                if "rgb" in batch
                else []
            )
            + [
                {
                    "type": "rgb",
                    "img": out["comp_rgb"][0],
                    "kwargs": {"data_format": "HWC"},
                },
            ]
            + (
                [
                    {
                        "type": "rgb",
                        "img": out["comp_normal"][0],
                        "kwargs": {"data_format": "HWC", "data_range": (0, 1)},
                    }
                ]
                if "comp_normal" in out
                else []
            )
            + [{"type": "grayscale", "img": out["depth"][0], "kwargs": {}}]
            + [
                {
                    "type": "grayscale",
                    "img": out["opacity"][0, :, :, 0],
                    "kwargs": {"cmap": None, "data_range": (0, 1)},
                },
            ],
        )

    def on_test_epoch_end(self):
        self.save_img_sequence(
            f"it{self.true_global_step}-test",
            f"it{self.true_global_step}-test",
            "(\d+)\.png",
            save_format="mp4",
            fps=30,
        )
