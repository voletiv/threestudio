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

    def log_loss(self, name, loss_term, weight=1.0):
        self.log(name, loss_term)
        loss_term_weighted = weight * loss_term
        self.log(name+"_w", loss_term_weighted) 
        return loss_term_weighted

    def training_substep(self, batch, batch_idx, do_ref, loss_weight=1.0):
        # opt = self.optimizers()
        # opt.zero_grad()
        
        for name, value in self.cfg.loss.items():
            self.log(f"train_params/{name}", self.C(value))

        loss = 0.0

        if do_ref:
            # bg_color = torch.rand_like(batch['rays_o'])
            ambient_ratio = 1.0
            shading = "diffuse"
            batch["shading"] = shading
            bg_color = None
        else:
            batch = batch["random_camera"]
            # if random.random() > 0.5:
            #     bg_color = None
            # else:
            bg_color = torch.rand(3).to(self.device)
            ambient_ratio = 0.3 + 0.7 * random.random()

        batch["bg_color"] = bg_color
        batch["ambient_ratio"] = ambient_ratio

        out = self(batch)

        substep_name = 'ref' if do_ref else 'zero123'
        def log_loss2(shortname, loss, weight):
            return self.log_loss(f"train/loss_{substep_name}_{shortname}", loss, weight)

        if do_ref:
            gt_mask = batch["mask"]
            gt_rgb = batch["rgb"]
            gt_depth = batch["depth"]

            # color loss
            gt_rgb = gt_rgb * gt_mask.float() + out["comp_rgb_bg"] * (1 - gt_mask.float())
            loss += log_loss2("rgb", F.mse_loss(gt_rgb, out["comp_rgb"]), self.C(self.cfg.loss.lambda_rgb))
            
            # mask loss
            loss_mask = F.mse_loss(gt_mask.float(), out["opacity"])
            loss += log_loss2("mask", loss_mask, self.C(self.cfg.loss.lambda_mask))
            
            # opacity_clamped = out['opacity'].clamp(1e-3, 1-1e-3)
            # gt_mask_clamped = gt_mask.float().clamp(1e-3, 1-1e-3)
            # loss += self.log_loss("train/loss_mask_clamped", binary_cross_entropy(gt_mask_clamped, opacity_clamped), self.C(self.cfg.loss.lambda_mask))
            
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
                loss += log_loss2("depth", F.mse_loss(valid_gt_depth, valid_pred_depth), self.C(self.cfg.loss.lambda_depth))
        else:
            cond = self.guidance.get_cond(**batch)
            guidance_out = self.guidance(out["comp_rgb"], cond, rgb_as_latents=False)
            loss += log_loss2("guidance", guidance_out["sds"], self.C(self.cfg.loss.lambda_sds)) # claforte: TODO: check if there's a lambda for that
            
        if self.true_global_step % 10 == 0:
            with torch.no_grad():
                self.save_image_grid(
                    f"it{self.true_global_step}-{substep_name}-debug.png",
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

        if self.C(self.cfg.loss.lambda_orient) > 0:
            if "normal" not in out:
                raise ValueError("Normal is required for orientation loss, no normal is found in the output.")
            loss_orient = (
                out["weights"].detach()
                * dot(out["normal"], out["t_dirs"]).clamp_min(0.0) ** 2
            ).sum() / (out["opacity"] > 0).sum()  # claforte: not sure how this scales with # of samples or pixels... might need a `mean()`` in there...
            loss += log_loss2("orient", loss_orient, self.C(self.cfg.loss.lambda_orient))
            
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
            loss += log_loss2("normal_smooth", loss_normal_smooth, self.C(self.cfg.loss.lambda_normal_smooth))
            
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
            loss += log_loss2("3d_normal_smooth", loss_3d_normal_smooth, self.C(self.cfg.loss.lambda_3d_normal_smooth))
            
        loss_sparsity = (out["opacity"] ** 2 + 0.01).sqrt().mean()
        loss += log_loss2("sparsity", loss_sparsity, self.C(self.cfg.loss.lambda_sparsity))
        
        opacity_clamped = out["opacity"].clamp(1.0e-3, 1.0 - 1.0e-3)
        loss_opaque = binary_cross_entropy(opacity_clamped, opacity_clamped)
        loss += log_loss2("opaque", loss_opaque, self.C(self.cfg.loss.lambda_opaque))
        
        log_loss2("", loss, loss_weight)

        return {"loss": loss}
        
    def training_step(self, batch, batch_idx):
        out_no_ref = self.training_substep(batch, batch_idx, do_ref=False, loss_weight=1.0)
        # do_ref = (
        #     self.true_global_step < self.cfg.freq.ref_only_steps
        #     or self.true_global_step % self.cfg.freq.n_ref == 0
        # )
        out_ref = self.training_substep(batch, batch_idx, do_ref=True, loss_weight=1.0)
        total_loss = out_no_ref["loss"] + out_ref["loss"]
        self.log("train/loss", total_loss, prog_bar=True)
        
        #sch = self.lr_schedulers()
        #sch.step()

        
        return {"loss": total_loss}

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
