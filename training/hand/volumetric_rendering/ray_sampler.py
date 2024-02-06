# SPDX-FileCopyrightText: Copyright (c) 2021-2022 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.

"""
The ray sampler is a module that takes in camera matrices and resolution and batches of rays.
Expects cam2world matrices that use the OpenCV camera coordinate system conventions.
"""

import torch

intrinsics_dry_run = torch.tensor([[31.25,  0.0, 0.5],
                                    [0.0, 31.25, 0.5],
                                    [0.0, 0.0, 1.0]])

camera2world_dry_run = torch.tensor([[7.1037e-03, -8.6363e-01,  5.0408e-01, -4.6887e+00],
                                    [-8.4197e-01, -2.7712e-01, -4.6292e-01, 3.6448e+00],
                                    [5.3948e-01, -4.2113e-01, -7.2911e-01, 5.6236e+00],
                                    [0.0000e+00, 0.0000e+00, 0.0000e+00, 1.0000e+00]])

class RaySampler(torch.nn.Module):
    def __init__(self):
        super().__init__()
        self.ray_origins_h, self.ray_directions, self.depths, self.image_coords, self.rendering_options = None, None, None, None, None


    def forward(self, cam2world_matrix, intrinsics, resolution):
        """
        Create batches of rays and return origins and directions.

        cam2world_matrix: (N, 4, 4)
        intrinsics: (N, 3, 3)
        resolution: int

        ray_origins: (N, M, 3)
        ray_dirs: (N, M, 2)
        """
        N, M = cam2world_matrix.shape[0], resolution**2
        cam_locs_world = cam2world_matrix[:, :3, 3]
        fx = intrinsics[:, 0, 0]
        fy = intrinsics[:, 1, 1]
        cx = intrinsics[:, 0, 2]
        cy = intrinsics[:, 1, 2]
        sk = intrinsics[:, 0, 1]

        uv = torch.stack(torch.meshgrid(torch.arange(resolution, dtype=torch.float32, device=cam2world_matrix.device), torch.arange(resolution, dtype=torch.float32, device=cam2world_matrix.device), indexing='ij')) * (1./resolution) + (0.5/resolution)
        uv = uv.flip(0).reshape(2, -1).transpose(1, 0)
        uv = uv.unsqueeze(0).repeat(cam2world_matrix.shape[0], 1, 1)

        x_cam = uv[:, :, 0].view(N, -1)
        y_cam = uv[:, :, 1].view(N, -1)
        z_cam = torch.ones((N, M), device=cam2world_matrix.device)

        x_lift = (x_cam - cx.unsqueeze(-1) + cy.unsqueeze(-1)*sk.unsqueeze(-1)/fy.unsqueeze(-1) - sk.unsqueeze(-1)*y_cam/fy.unsqueeze(-1)) / fx.unsqueeze(-1) * z_cam
        y_lift = (y_cam - cy.unsqueeze(-1)) / fy.unsqueeze(-1) * z_cam

        cam_rel_points = torch.stack((x_lift, y_lift, z_cam, torch.ones_like(z_cam)), dim=-1)

        world_rel_points = torch.bmm(cam2world_matrix, cam_rel_points.permute(0, 2, 1)).permute(0, 2, 1)[:, :, :3]

        ray_dirs = world_rel_points - cam_locs_world[:, None, :]
        ray_dirs = torch.nn.functional.normalize(ray_dirs, dim=2)

        ray_origins = cam_locs_world.unsqueeze(1).repeat(1, ray_dirs.shape[1], 1)

        return ray_origins, ray_dirs


class AvatarGenRaySampler(torch.nn.Module):
    def __init__(self):
        super().__init__()
        self.ray_origins_h, self.ray_directions, self.depths, self.image_coords, self.rendering_options = None, None, None, None, None
    def forward(self, cam2world_matrix, intrinsics, resolution):
        device = cam2world_matrix.device
        im_width_res = im_height_res = resolution

        if torch.all(intrinsics==0).item():
            intrinsics = intrinsics_dry_run.repeat(intrinsics.shape[0], 1, 1).to(device)
            cam2world_matrix = camera2world_dry_run.repeat(intrinsics.shape[0], 1, 1).to(device)

        M = torch.eye(4, device=device)
        M[1, 1] *= -1
        M[2, 2] *= -1
        cam2world_matrix = torch.bmm(cam2world_matrix, M.unsqueeze(0).repeat(cam2world_matrix.shape[0], 1, 1))
        focal = torch.cat([intrinsics[:, 0, 0:1], intrinsics[:, 1, 1:2]], dim=1).unsqueeze(1) * resolution
        center = torch.cat([intrinsics[:, 0, 2:3], intrinsics[:, 1, 2:3]], dim=1).unsqueeze(1) * resolution

        i, j = torch.meshgrid(torch.linspace(0.5, im_width_res - 0.5, im_width_res, device=device),
                            torch.linspace(0.5, im_height_res - 0.5, im_height_res, device=device),
                            indexing='ij')
        i = i.t().unsqueeze(0)
        j = j.t().unsqueeze(0)

        # if center is None:
        #     center = torch.from_numpy(np.array([[im_width_res*.5, im_height_res*.5]], dtype=np.float32)).to(device).unsqueeze(0)
        dirs = torch.stack([
            (i - center[..., :1]) / focal[..., :1],
            -(j - center[..., 1:]) / focal[..., 1:],
            -torch.ones_like(i).expand(focal.shape[0], im_height_res,im_width_res)], -1)
        dirs = dirs / torch.norm(dirs, dim=-1, keepdim=True)
        # Rotate ray directions from camera frame to the world frame
        rays_d = torch.sum(dirs[..., None, :] * cam2world_matrix[:, None, None, :3, :3], -1)  # dot product, equals to: [cam2world_matrix.dot(dir) for dir in dirs]
        # Translate camera frame's origin to the world frame. It is the origin of all rays.
        rays_o = cam2world_matrix[:, None, None, :3, -1].expand(rays_d.shape)
        B,H,W,C = rays_d.shape
        return rays_o.reshape(B,H*W,C), rays_d.reshape(B,H*W,C)