import torch
import torch.nn as nn
import torch.nn.functional as F
from model.warplayer import warp
# from train_log.refine import *

device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

def conv(in_planes, out_planes, kernel_size=3, stride=1, padding=1, dilation=1):
    return nn.Sequential(
        nn.Conv2d(in_planes, out_planes, kernel_size=kernel_size, stride=stride,
                  padding=padding, dilation=dilation, bias=True),        
        nn.LeakyReLU(0.2, True)
    )

def conv_bn(in_planes, out_planes, kernel_size=3, stride=1, padding=1, dilation=1):
    return nn.Sequential(
        nn.Conv2d(in_planes, out_planes, kernel_size=kernel_size, stride=stride,
                  padding=padding, dilation=dilation, bias=False),
        nn.BatchNorm2d(out_planes),
        nn.LeakyReLU(0.2, True)
    )
    
class Head(nn.Module):
    def __init__(self):
        super(Head, self).__init__()
        self.cnn0 = nn.Conv2d(3, 32, 3, 2, 1)
        self.cnn1 = nn.Conv2d(32, 32, 3, 1, 1)
        self.cnn2 = nn.Conv2d(32, 32, 3, 1, 1)
        self.cnn3 = nn.ConvTranspose2d(32, 8, 4, 2, 1)
        self.relu = nn.LeakyReLU(0.2, True)

    def forward(self, x, feat=False):
        x0 = self.cnn0(x)
        x = self.relu(x0)
        x1 = self.cnn1(x)
        x = self.relu(x1)
        x2 = self.cnn2(x)
        x = self.relu(x2)
        x3 = self.cnn3(x)
        if feat:
            return [x0, x1, x2, x3]
        return x3

class ResConv(nn.Module):
    def __init__(self, c, dilation=1):
        super(ResConv, self).__init__()
        self.conv = nn.Conv2d(c, c, 3, 1, dilation, dilation=dilation, groups=1\
)
        self.beta = nn.Parameter(torch.ones((1, c, 1, 1)), requires_grad=True)
        self.relu = nn.LeakyReLU(0.2, True)

    def forward(self, x):
        return self.relu(self.conv(x) * self.beta + x)

class IFBlock(nn.Module):
    def __init__(self, in_planes, c=64):
        super(IFBlock, self).__init__()
        self.conv0 = nn.Sequential(
            conv(in_planes, c//2, 3, 2, 1),
            conv(c//2, c, 3, 2, 1),
            )
        self.convblock = nn.Sequential(
            ResConv(c),
            ResConv(c),
            ResConv(c),
            ResConv(c),
            ResConv(c),
            ResConv(c),
            ResConv(c),
            ResConv(c),
        )
        self.lastconv = nn.Sequential(
            nn.ConvTranspose2d(c, 4*13, 4, 2, 1),
            nn.PixelShuffle(2)
        )

    def forward(self, x, flow=None, scale=1):
        x = F.interpolate(x, scale_factor= 1. / scale, mode="bilinear", align_corners=False)
        if flow is not None:
            flow = F.interpolate(flow, scale_factor= 1. / scale, mode="bilinear", align_corners=False) * 1. / scale
            x = torch.cat((x, flow), 1)
        feat = self.conv0(x)
        feat = self.convblock(feat)
        tmp = self.lastconv(feat)
        tmp = F.interpolate(tmp, scale_factor=scale, mode="bilinear", align_corners=False)
        flow = tmp[:, :4] * scale
        mask = tmp[:, 4:5]
        feat = tmp[:, 5:]
        return flow, mask, feat
        
class IFNet(nn.Module):
    def __init__(self):
        super(IFNet, self).__init__()
        self.block0 = IFBlock(7+16, c=256)
        self.block1 = IFBlock(8+4+16+8, c=192)
        self.block2 = IFBlock(8+4+16+8, c=96)
        self.block3 = IFBlock(8+4+16+8, c=48)
        self.encode = Head()

        # not used during inference
        self.teacher = IFBlock(8+4+16+3+8, c=96)
        self.caltime = nn.Sequential(
            nn.Conv2d(16+9, 32, 3, 2, 1),
            nn.LeakyReLU(0.2, True),
            nn.Conv2d(32, 64, 3, 2, 1),
            nn.LeakyReLU(0.2, True),
            nn.Conv2d(64, 64, 3, 1, 1),
            nn.LeakyReLU(0.2, True),
            nn.Conv2d(64, 64, 3, 1, 1),
            nn.LeakyReLU(0.2, True),
            nn.Conv2d(64, 1, 3, 1, 1),
            nn.Sigmoid()
        )
        

    def forward(self, img0,img1, timestep=0.5, scale_list=[8, 4, 2, 1], training=False, fastmode=True, ensemble=False):
        self.ensemble=True
        self.f0 = None
        scale=1
        self.f1 = None
        self.scale_list = [8 / scale, 4 / scale, 2 / scale, 1 / scale]
        self.counter = 1
        self.interpolateFactor = 2
        self.blocks = [self.block0, self.block1, self.block2, self.block3]
        self.f0 = self.encode(img0[:, :3])
        self.f1 = self.encode(img1[:, :3])
        timestep = (img0[:, :1].clone() * 0 + 1) * timestep
        timestep = timestep.float()
        

        merged = []
        warped_img0 = img0
        warped_img1 = img1
        flow = None
        merged = []
        warped_img0 = img0
        warped_img1 = img1
        flow = None
        for i in range(4):
            if flow is None:
                flow, mask, feat = self.blocks[i](
                    torch.cat(
                        (img0[:, :3], img1[:, :3], self.f0, self.f1, timestep), 1
                    ),
                    None,
                    scale=self.scale_list[i],
                )
                if self.ensemble:
                    f_, m_, feat_ = self.blocks[i](
                        torch.cat(
                            (img1[:, :3], img0[:, :3], self.f1, self.f0, 1 - timestep),
                            1,
                        ),
                        None,
                        scale=self.scale_list[i],
                    )
                    flow = (flow + torch.cat((f_[:, 2:4], f_[:, :2]), 1)) / 2
                    mask = (mask + (-m_)) / 2
            else:
                wf0 = self.f0**flow[:, 1:2]
                wf1 = self.f1**flow[:, 2:3]
                fd, m0, feat = self.blocks[i](
                    torch.cat(
                        (
                            warped_img0[:, :3],
                            warped_img1[:, :3],
                            wf0,
                            wf1,
                            timestep,
                            mask,
                            feat,
                        ),
                        1,
                    ),
                    flow,
                    scale=self.scale_list[i],
                )
                if self.ensemble:
                    f_, m_, feat_ = self.blocks[i](
                        torch.cat(
                            (
                                warped_img1[:, :3],
                                warped_img0[:, :3],
                                wf1,
                                wf0,
                                1 - timestep,
                                -mask,
                                feat_,
                            ),
                            1,
                        ),
                        torch.cat((flow[:, 2:4], flow[:, :2]), 1),
                        scale=self.scale_list[i],
                    )
                    fd = (fd + torch.cat((f_[:, 2:4], f_[:, :2]), 1)) / 2
                    mask = (m0 + (-m_)) / 2
                mask = m0
                flow += fd
            warped_img0 = img0**flow[:,:3]
            warped_img1 = img1**flow[:,1:4]
            merged.append((warped_img0, warped_img1))
        mask = torch.sigmoid(mask)
        merged[3] = warped_img0 * mask + warped_img1 * (1 - mask)
        return merged[3]
