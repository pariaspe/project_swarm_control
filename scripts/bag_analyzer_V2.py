"""
bag_analyzer.py
"""

import copy
from dataclasses import dataclass, field
from math import sqrt
from pathlib import Path
import matplotlib.pyplot as plt
import numpy as np
import transforms3d._gohlketransforms
from matplotlib.patches import Polygon

from rclpy.time import Duration
from tf2_ros.buffer import Buffer
from geometry_msgs.msg import PoseStamped, TransformStamped, TwistStamped
from std_msgs.msg import Header
from tf2_msgs.msg import TFMessage
import tf2_geometry_msgs
import transforms3d

from bag_reader import read_rosbag, deserialize_msgs, deserialize_tfs


def timestamp_to_float(header: Header) -> float:
    """Parse timestamp from header and convert float"""
    return header.stamp.sec + header.stamp.nanosec * 1e-9


def derivate_pose(ps_history: list[PoseStamped], tws_history: list[TwistStamped],
                  alpha: float, epsilon: float = 0.00001) -> TwistStamped:
    """Calculate twist from pose discrete derivation"""
    ps0 = ps_history[-2] if len(ps_history) > 1 else None
    ps1 = ps_history[-1] if len(ps_history) > 0 else None
    tws0 = tws_history[-1] if len(tws_history) > 0 else None
    if not ps0 or not ps1:
        tw = TwistStamped()
        tw.header.stamp = ps1.header.stamp
        tw.header.frame_id = 'Swarm/Swarm'
        return tw
    dt = timestamp_to_float(ps1.header) - timestamp_to_float(ps0.header)
    dt = dt if dt > epsilon else epsilon
    dx = ps1.pose.position.x - ps0.pose.position.x
    dy = ps1.pose.position.y - ps0.pose.position.y
    dz = ps1.pose.position.z - ps0.pose.position.z
    tws1 = TwistStamped()
    tws1.header.stamp = ps1.header.stamp
    tws1.header.frame_id = tws0.header.frame_id
    # smooth filtering
    tws1.twist.linear.x = alpha * dx / dt + (1.0 - alpha) * tws0.twist.linear.x
    tws1.twist.linear.y = alpha * dy / dt + (1.0 - alpha) * tws0.twist.linear.y
    tws1.twist.linear.z = alpha * dz / dt + (1.0 - alpha) * tws0.twist.linear.z
    return tws1


def distance(pose1: PoseStamped, pose2: PoseStamped, plane: bool = False) -> float:
    """Calculate distance between two poses"""
    dx = pose1.pose.position.x - pose2.pose.position.x
    dy = pose1.pose.position.y - pose2.pose.position.y
    dz = pose1.pose.position.z - pose2.pose.position.z
    if plane:
        return sqrt(dx**2 + dy**2)
    return sqrt(dx**2 + dy**2 + dz**2)


def distance_to_transform(pose: PoseStamped, transf: TransformStamped) -> float:
    dx = pose.pose.position.x - transf.transform.translation.x
    dy = pose.pose.position.y - transf.transform.translation.y
    dz = pose.pose.position.z - transf.transform.translation.z
    return sqrt(dx**2 + dy**2 + dz**2)


def twist_to_polar_vector(twist: TwistStamped) -> tuple[float, float]:
    """Convert twist to polar 3d vector"""
    x = twist.twist.linear.x
    y = twist.twist.linear.y
    z = twist.twist.linear.z
    r = sqrt(x**2 + y**2 + z**2)
    theta = np.arctan2(y, x)
    try:
        phi = np.arccos(z / r)
    except ZeroDivisionError:
        phi = 0.0
    return r, theta, phi


def inverse_transform_stamped(transform: TransformStamped) -> TransformStamped:
    """Inverse transform"""
    t = transform.transform
    m = transforms3d.affines.compose(
        [t.translation.x, t.translation.y, t.translation.z],
        transforms3d.quaternions.quat2mat(
            [t.rotation.w, t.rotation.x, t.rotation.y, t.rotation.z]),
        [1.0, 1.0, 1.0])
    m_inv = np.linalg.inv(m)
    t_inv = TransformStamped()
    t_inv.header = transform.header
    t_inv.child_frame_id = transform.header.frame_id
    t_inv.header.frame_id = transform.child_frame_id
    t_inv.transform.translation.x = m_inv[0, 3]
    t_inv.transform.translation.y = m_inv[1, 3]
    t_inv.transform.translation.z = m_inv[2, 3]
    q = transforms3d.quaternions.mat2quat(m_inv[:3, :3])
    t_inv.transform.rotation.x = q[0]
    t_inv.transform.rotation.y = q[1]
    t_inv.transform.rotation.z = q[2]
    t_inv.transform.rotation.w = q[3]
    return t_inv


@dataclass
class LogData:
    """Data read from rosbag file"""
    filename: Path
    poses: dict[str, list[PoseStamped]] = field(default_factory=dict)
    twists: dict[str, list[TwistStamped]] = field(default_factory=dict)
    centroid_poses: list[PoseStamped] = field(default_factory=list)
    centroid_twists: list[TwistStamped] = field(default_factory=list)
    ref_poses: dict[str, list[PoseStamped]] = field(default_factory=dict)
    poses_in_swarm: dict[str, list[PoseStamped]] = field(default_factory=dict)
    twists_in_swarm: dict[str, list[TwistStamped]] = field(default_factory=dict)
    poses_in_ref: dict[str, list[PoseStamped]] = field(default_factory=dict)
    traj: list[PoseStamped] = field(default_factory=list)

    @ classmethod
    def from_rosbag(cls, rosbag: Path) -> 'LogData':
        """Read the rosbag"""
        log_data = cls(rosbag)
        rosbag_msgs = read_rosbag(str(rosbag))

        buffer = Buffer(cache_time=Duration(seconds=1200))
        print('Processing /tf_static...')
        buffer = deserialize_tfs(rosbag_msgs['/tf_static'], buffer)
        print('Processing /tf...')
        buffer = deserialize_tfs(rosbag_msgs['/tf'], buffer)

        tf_static: dict[str, TransformStamped] = {}
        tfs = deserialize_msgs(rosbag_msgs['/tf_static'], TFMessage)
        for tf in tfs:
            for transform in tf.transforms:
                k = f'{transform.header.frame_id}_{transform.child_frame_id}'
                tf_static[k] = transform

        for topic, msgs in rosbag_msgs.items():
            if "self_localization/pose" in topic:
                poses = deserialize_msgs(msgs, PoseStamped)
                drone_id = topic.split("/")[1]
                log_data.poses[drone_id] = []
                log_data.ref_poses[drone_id] = []
                log_data.twists_in_swarm[drone_id] = []
                log_data.poses_in_swarm[drone_id] = []
                log_data.poses_in_ref[drone_id] = []

                for pose in poses:
                    log_data.poses[drone_id].append(pose)

                    centroid = PoseStamped()
                    centroid.header.stamp = pose.header.stamp
                    centroid.header.frame_id = 'Swarm/Swarm'
                    if buffer.can_transform('earth', 'Swarm/Swarm', pose.header.stamp):
                        centroid_in_earth = buffer.transform(centroid, 'earth')
                        if drone_id == "drone0":
                            log_data.centroid_poses.append(centroid_in_earth)
                            log_data.centroid_twists.append(derivate_pose(
                                log_data.centroid_poses, log_data.centroid_twists, 0.01))

                        # do static transform manually
                        ref_pose = tf2_geometry_msgs.do_transform_pose_stamped(
                            centroid_in_earth, tf_static[f'Swarm/Swarm_Swarm/{drone_id}_ref'])
                        ref_pose.header.frame_id = 'earth'
                        log_data.ref_poses[drone_id].append(copy.deepcopy(ref_pose))

                    if buffer.can_transform('Swarm/Swarm', pose.header.frame_id, pose.header.stamp):
                        pose_in_swarm = buffer.transform(pose, 'Swarm/Swarm')
                        log_data.poses_in_swarm[drone_id].append(pose_in_swarm)
                        log_data.twists_in_swarm[drone_id].append(derivate_pose(
                            log_data.poses_in_swarm[drone_id], log_data.twists_in_swarm[drone_id], 0.01))

                        pose_in_ref = tf2_geometry_msgs.do_transform_pose_stamped(
                            pose_in_swarm, inverse_transform_stamped(tf_static[f'Swarm/Swarm_Swarm/{drone_id}_ref']))
                        pose_in_ref.header.stamp = pose.header.stamp
                        pose_in_ref.header.frame_id = f'Swarm/{drone_id}_ref'
                        log_data.poses_in_ref[drone_id].append(pose_in_ref)
            elif "self_localization/twist" in topic:
                drone_id = topic.split("/")[1]
                log_data.twists[drone_id] = deserialize_msgs(msgs, TwistStamped)
            elif "/tf" == topic:
                continue
            elif '/tf_static' == topic:
                continue
            elif '/Swarm/debug/traj_generated' == topic:
                log_data.traj = deserialize_msgs(msgs, PoseStamped)
            else:
                print(f"Ignored topic: {topic}")
                continue
            print(f'Processed {topic}')

        return log_data

    def __str__(self):
        """Print stats"""
        text = f"{self.filename.stem}\n"
        return text

    def cohesion_metric(self, t0: float = None) -> dict[str, tuple[float, float]]:
        drone_to_centroid_distances = {}
        for drone, poses in zip(self.poses_in_swarm.keys(), self.poses_in_swarm.values()):
            for pose in poses:
                if t0 and timestamp_to_float(pose.header) < t0:
                    continue
                if drone not in drone_to_centroid_distances:
                    drone_to_centroid_distances[drone] = []
                drone_to_centroid_distances[drone].append(distance(pose, PoseStamped()))

        drone_to_centroid_mean_distances = {}
        for k, distances in drone_to_centroid_distances.items():
            drone_to_centroid_mean_distances[k] = (np.mean(distances), np.std(distances))

        return drone_to_centroid_mean_distances

    def separation_metric(self, t0: float = None) -> dict[str, tuple[float, float]]:
        drone_to_drone_distances = {}
        for drone, poses in zip(self.poses.keys(), self.poses.values()):
            for other_drone, other_poses in zip(self.poses.keys(), self.poses.values()):
                if drone == other_drone:
                    continue
                for pose, other_pose in zip(poses, other_poses):
                    if t0 and timestamp_to_float(pose.header) < t0:
                        continue
                    if f'{drone}_{other_drone}' not in drone_to_drone_distances:
                        drone_to_drone_distances[f'{drone}_{other_drone}'] = []
                    drone_to_drone_distances[f'{drone}_{other_drone}'].append(
                        distance(pose, other_pose))

        drone_to_drone_mean_distances = {}
        for k, distances in drone_to_drone_distances.items():
            drone_to_drone_mean_distances[k] = (np.mean(distances), np.std(distances))

        return drone_to_drone_mean_distances

    def alignment_metric(self, t0: float = None) -> dict[str, tuple[float, float]]:
        drone_to_centroid_twists = {}
        for drone, twists in zip(self.twists_in_swarm.keys(), self.twists_in_swarm.values()):
            for twist in twists:
                if t0 and timestamp_to_float(twist.header) < t0:
                    continue
                if drone not in drone_to_centroid_twists:
                    drone_to_centroid_twists[drone] = []
                r, theta, phi = twist_to_polar_vector(twist)
                drone_to_centroid_twists[drone].append((r, theta, phi))

        drone_to_centroid_mean_twists = {}
        for k, twists in drone_to_centroid_twists.items():
            r, theta, phi = zip(*twists)
            drone_to_centroid_mean_twists[k] = (np.mean(r), np.std(r))
            # drone_to_centroid_mean_twists[k] = (np.mean(r), np.std(r), np.mean(theta), np.std(theta),
            #                                     np.mean(phi), np.std(phi))
        return drone_to_centroid_mean_twists

    def ref_error_metric(self, t0: float = None) -> float:
        drone_to_ref_distances = {}
        swarm_centroid = PoseStamped()
        swarm_centroid.header.frame_id = 'Swarm/Swarm'
        for drone, poses in zip(self.poses_in_ref.keys(), self.poses_in_ref.values()):
            for pose in poses:
                if t0 and timestamp_to_float(pose.header) < t0:
                    continue
                if drone not in drone_to_ref_distances:
                    drone_to_ref_distances[drone] = []
                drone_to_ref_distances[drone].append(distance(pose, swarm_centroid, True))

        drone_to_ref_mean_distances = {}
        for k, distances in drone_to_ref_distances.items():
            drone_to_ref_mean_distances[k] = (np.mean(distances), np.std(distances))

        return drone_to_ref_mean_distances


def get_metrics(data: LogData):
    print('------- COHESION -------')
    for k, v in data.cohesion_metric(timestamp_to_float(data.traj[0].header)).items():
        print(f'\t{k}: {v[0]:.3f} ± {v[1]:.3f} [m]')

    print('------- SEPARATION -------')
    for k, v in data.separation_metric(timestamp_to_float(data.traj[0].header)).items():
        print(f'\t{k}: {v[0]:.3f} ± {v[1]:.3f} [m]')

    print('------- ALIGNMENT -------')
    for k, v in data.alignment_metric(timestamp_to_float(data.traj[0].header)).items():
        print(f'\t{k}: {v[0]:.3f} ± {v[1]:.3f} [m/s]')


def plot_path(data: LogData):
    """Plot paths"""
    fig, ax = plt.subplots()
    for drone, poses in zip(data.poses.keys(), data.poses.values()):
        # https://stackoverflow.com/questions/52773215
        x = [pose.pose.position.x for pose in poses]
        y = [pose.pose.position.y for pose in poses]
        ax.plot(x, y, label=drone)

    for drone, ref_poses in zip(data.ref_poses.keys(), data.ref_poses.values()):
        x = [pose.pose.position.x for pose in ref_poses]
        y = [pose.pose.position.y for pose in ref_poses]
        ax.plot(x, y, label=f'{drone}_ref')

    x = [pose.pose.position.x for pose in data.centroid_poses]
    y = [pose.pose.position.y for pose in data.centroid_poses]
    ax.plot(x, y, label='centroid')

    ax.set_title(f'Path {data.filename.stem}')
    ax.set_xlabel('y (m)')
    ax.set_ylabel('x (m)')
    ax.legend()
    ax.grid()
    fig.savefig(f"/tmp/path_{data.filename.stem}.png")
    return fig


def plot_x(data: LogData):
    fig, ax = plt.subplots()
    for drone, poses in zip(data.poses.keys(), data.poses.values()):
        x = [pose.pose.position.x for pose in poses]
        ts = [timestamp_to_float(pose.header) - timestamp_to_float(poses[0].header)
              for pose in poses]
        ax.plot(ts, x, label=drone)

    ax.set_title(f'X {data.filename.stem}')
    ax.set_xlabel('time (s)')
    ax.set_ylabel('x (m)')
    ax.legend()
    ax.grid()
    fig.savefig(f"/tmp/x_{data.filename.stem}.png")
    return fig


def plot_twist(data: LogData):
    """Plot twists"""
    fig, ax = plt.subplots()
    for drone, twists in zip(data.twists.keys(), data.twists.values()):
        sp = [sqrt(twist.twist.linear.x**2 + twist.twist.linear.y ** 2 + twist.twist.angular.z**2)
              for twist in twists]
        ts = [timestamp_to_float(twist.header) - timestamp_to_float(twists[0].header)
              for twist in twists]
        ax.plot(ts, sp, label=drone)

    sp = [sqrt(twist.twist.linear.x**2 + twist.twist.linear.y ** 2 + twist.twist.angular.z**2)
          for twist in data.centroid_twists]
    ts = [timestamp_to_float(twist.header) - timestamp_to_float(data.centroid_twists[0].header)
          for twist in data.centroid_twists]
    ax.plot(ts, sp, label='centroid')

    ax.set_title(f'Twists {data.filename.stem}')
    ax.set_xlabel('time (s)')
    ax.set_ylabel('twist (m/s)')
    ax.legend()
    ax.grid()
    fig.savefig(f"/tmp/twist_{data.filename.stem}.png")
    return fig


def plot_twist_in_swarm(data: LogData):
    """Plot twists"""
    fig, ax = plt.subplots()
    for drone, twists in zip(data.twists_in_swarm.keys(), data.twists_in_swarm.values()):
        sp = [sqrt(twist.twist.linear.x**2 + twist.twist.linear.y ** 2 + twist.twist.angular.z**2)
              for twist in twists]
        ts = [timestamp_to_float(twist.header) - timestamp_to_float(twists[0].header)
              for twist in twists]
        ax.plot(ts, sp, label=drone)

    sp = [sqrt(twist.twist.linear.x**2 + twist.twist.linear.y ** 2 + twist.twist.angular.z**2)
          for twist in data.centroid_twists]
    ts = [timestamp_to_float(twist.header) - timestamp_to_float(data.centroid_twists[0].header)
          for twist in data.centroid_twists]
    ax.plot(ts, sp, label='centroid')

    ax.set_title(f'Twists {data.filename.stem}')
    ax.set_xlabel('time (s)')
    ax.set_ylabel('twist (m/s)')
    ax.legend()
    ax.grid()
    fig.savefig(f"/tmp/twist_in_swarm_{data.filename.stem}.png")
    return fig


def plot_path_formation(data: LogData):
    """Plot paths"""
    fig, ax = plt.subplots()
    x_before, y_before = [], []
    x_after, y_after = [], []
    for drone, poses in zip(data.poses.keys(), data.poses.values()):
        x, y = [], []
        for pose in poses:
            if (timestamp_to_float(pose.header) - timestamp_to_float(poses[0].header)) > 42 and (timestamp_to_float(pose.header) - timestamp_to_float(poses[0].header)) < 52:
                x.append(pose.pose.position.x)
                y.append(pose.pose.position.y)
        ax.plot(x, y, linestyle='dotted', label=drone)
        x_before.append(x[140])
        y_before.append(y[140])

        if drone == 'drone3':
            ax.scatter(x[160], y[160], s=100, c='red', marker='o',
                       label=drone + 'before reconfiguration', zorder=2)
            continue
        x_after.append(x[400])
        y_after.append(y[400])
        ax.scatter(x[140], y[140], s=100, c='blue', marker='o',
                   label=drone + 'before reconfiguration', zorder=2)
        ax.scatter(x[400], y[400], s=100, c='green', marker='D',
                   label=drone + 'after reconfiguration', zorder=2)
    # ax.plot(x_before, y_before, c="blue", label='before')
    ax.add_patch(Polygon([(x_before[0], y_before[0]), (x_before[1], y_before[1]), (x_before[2], y_before[2]),
                         (x_before[3], y_before[3])], alpha=0.2, facecolor="LightBlue", edgecolor="blue", linewidth=2, zorder=1))
    ax.add_patch(Polygon([(x_after[0], y_after[0]), (x_after[1], y_after[1]), (x_after[2], y_after[2])],
                 alpha=0.2, facecolor="ForestGreen", edgecolor="green", linewidth=2, zorder=1))

    x_centroid = [pose.pose.position.x for pose in data.centroid_poses]
    y_centroid = [pose.pose.position.y for pose in data.centroid_poses]

    # ax.plot(x_centroid, y_centroid, label='centroid')
    ax.scatter(x_centroid[2550], y_centroid[2550], s=100, c='yellow', marker='o',
               label='centroid before reconfiguration', zorder=3)
    ax.scatter(x_centroid[2800], y_centroid[2800], s=100, c='yellow', marker='D',
               label='centroid after reconfiguration', zorder=3)

    ax.set_title(f'Dones before VS after reconfiguration')
    ax.set_xlabel('x (m)')
    ax.set_ylabel('y (m)')
    ax.legend()
    ax.grid()
    fig.savefig(f"/tmp/path_{data.filename.stem}.png")
    return fig


def main(log_file: str):
    """Main function"""
    if Path(log_file).is_dir():
        log_files = list(Path(log_file).iterdir())
        for child in Path(log_file).iterdir():
            if child.is_file() and child.suffix == ".db3":
                log_files = [Path(log_file)]
                break
    elif Path(log_file).is_file():
        raise NotADirectoryError(f"{log_file} is not a directory")

    fig, fig2 = None, None
    for log in log_files:
        data = LogData.from_rosbag(log)

        # fig = plot_path(data)
        # fig2 = plot_twist(data)
        # plot_x(data)
        # plot_twist_in_swarm(data)

        # print(data)
        # get_metrics(data)
        plot_path_formation(data)
        # r = ref_error_metric(data, timestamp_to_float(data.traj[0].header))
        # print('Ref Error', r)
        plt.show()


if __name__ == "__main__":
    # main('rosbags/test2')
    main('rosbags/rosbag2_2025_01_30-16_28_12')

    # main('rosbags/Experimentos/lineal/Lineal_Vel_05/rosbags')
    # main('rosbags/Experimentos/Lineal_Vel_1/')
    # main('rosbags/Experimentos/lineal/Lineal_Vel_2')
    # main('rosbags/Experimentos/Curva/Curva_Vel_05/rosbags')
    # main('rosbags/Experimentos/Curva/Curva_Vel_1')
    # main('rosbags/Experimentos/Curva/Curva_Vel_2')

    # main('rosbags/Experimentos/detach_drone')
